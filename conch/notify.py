"""Desktop notifications for long turns.

Only interactive sessions notify. Prefer a notification posted by the terminal
emulator itself. Apple Terminal has a tab bell but no notification OSC; for
a clickable banner it needs terminal-notifier. Other Linux desktop terminals
use notify-send (delivery only, not a portable window-focus API).
"""

from __future__ import annotations

import html
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Optional

DEFAULT_AFTER_SECONDS = 30.0
_MAX_MESSAGE_CHARS = 180


def notify_after_seconds(config: Optional[dict]) -> float:
    """The configured threshold in seconds; 0 means never notify. An
    unparseable value keeps the default rather than silently disabling."""
    if str((config or {}).get("notifications", "on")).strip().lower() in ("off", "0", "false", "no"):
        return 0.0
    raw = str((config or {}).get("notify_after_seconds", DEFAULT_AFTER_SECONDS)).strip().lower()
    if raw in ("", "0", "off", "false", "no", "never"):
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_AFTER_SECONDS


def _plain(text: str, limit: int) -> str:
    """One line of printable text: markdown emphasis and control characters
    removed, whitespace collapsed, cut with an ellipsis."""
    text = "".join(char if char.isprintable() else " " for char in str(text))
    text = " ".join(text.replace("**", "").replace("`", "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# Only terminal-notifier's click handler runs this. Match Terminal's tab.tty,
# not a title, tab index, or the window that happens to be frontmost. A closed
# terminal's device name can be recycled: check the original login session
# still owns it before asking Terminal to select anything.
_FOCUS_TERMINAL = '''on run argv
    set originTTY to item 1 of argv
    set sessionPID to (item 2 of argv) as integer
    set liveTTY to last word of (do shell script ("/bin/ps -p " & sessionPID & " -o tty="))
    if ("/dev/" & liveTTY) is not originTTY then return
    if application id "com.apple.Terminal" is not running then return
    tell application id "com.apple.Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if tty of t is originTTY then
                    set hostWindow to window id (id of w)
                    set selected of t to true
                    set miniaturized of hostWindow to false
                    set frontmost of hostWindow to true
                    activate
                    return
                end if
            end repeat
        end repeat
    end tell
end run'''


def _launch(argv: list[str], stdout: int = subprocess.DEVNULL) -> bool:
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except (OSError, UnicodeError):
        return False
    return True


def _tty_output(text: str) -> bool:
    # The controlling tty stays bound to the originating tab. Never emit OSC
    # into stdout (which may be captured) or wait on a flow-controlled tty.
    try:
        with open('/dev/tty', 'r+b', buffering=0) as tty:
            return tty.isatty() and _launch(['/usr/bin/printf', '%s', text], tty.fileno())
    except OSError:
        return False


def _apple_terminal(title: str, message: str) -> bool:
    notifier = shutil.which('terminal-notifier')
    if not notifier:
        # Stock Terminal marks the actual tab; sound/Dock badge are profile
        # settings. A bell is not a clickable Notification Center banner.
        return _tty_output('\a')
    try:
        tty = os.ttyname(sys.stdin.fileno())
        session = os.getsid(0)
    except (AttributeError, OSError, ValueError):
        return False
    if not re.fullmatch(r'/dev/ttys[0-9]+', tty):
        return False
    # -execute takes a shell string. Only our fixed script and OS-derived
    # identifiers go into it. The backslash on the two *display* fields is
    # terminal-notifier's own NSUserDefaults escape, including for '-flags'.
    click = shlex.join(['/usr/bin/osascript', '-e', _FOCUS_TERMINAL, tty, str(session)])
    return _launch([notifier, '-execute', click, '-title', '\\' + title, '-message', '\\' + message])


def send(title: str, message: str) -> bool:
    """Dispatch without waiting for delivery or a click. False = no dispatch."""
    platform = sys.platform
    env = os.environ
    if platform != 'darwin' and not platform.startswith('linux'):
        return False
    if any(env.get(key) for key in ('SSH_TTY', 'SSH_CONNECTION', 'SSH_CLIENT')):
        return False  # Don't post a remote conversation on the host desktop.
    title, message = _plain(title, 80), _plain(message, _MAX_MESSAGE_CHARS)
    program, term = env.get('TERM_PROGRAM'), env.get('TERM', '')
    # An outer emulator's variables can leak into an inner tmux/screen pane.
    # Don't send passthrough escapes or claim we can identify a mux client.
    multiplexed = env.get('TMUX') or env.get('STY') or term.startswith(('tmux', 'screen'))
    if not multiplexed and term != 'dumb':
        if (program in ('iTerm.app', 'ghostty', 'WezTerm', 'kitty')
                or term in ('xterm-kitty', 'xterm-ghostty')):
            # These emulators implement iTerm's OSC 9. Only a single field:
            # semicolons can introduce other OSC 9 operations (e.g. 9;4;…).
            text = f'{title}: {message}'.replace(';', ',')
            if _tty_output(f'\x1b]9;{text}\x1b\\'):
                return True
        elif platform == 'darwin' and program == 'Apple_Terminal':
            return _apple_terminal(title, message)
    if platform.startswith('linux') and (env.get('DISPLAY') or env.get('WAYLAND_DISPLAY')):
        notifier = shutil.which('notify-send')
        if notifier:
            # notify-send decodes backslash escapes in the body before the
            # desktop parses its markup. Quote both layers; '--' ends options.
            body = html.escape(message, quote=False).replace('\\', '\\\\')
            return _launch([notifier, '--app-name=Conch', '--', title, body])
    return False


def turn_finished(config: Optional[dict], elapsed: float, *, reply: str,
                  failed: bool, conversation: str) -> bool:
    """Notify that a turn ended when it ran past the threshold.

    The notification names the conversation and carries the reply's first
    line (or says the turn failed), so it is useful without switching back.
    """
    threshold = notify_after_seconds(config)
    if not threshold or elapsed < threshold:
        return False
    if failed:
        message = "The turn failed; details are in the terminal."
    else:
        first = next((line for line in (reply or "").splitlines() if line.strip()), "")
        message = first or "Finished with no reply text."
    minutes, seconds = divmod(int(elapsed), 60)
    took = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
    return send(f"Conch · {conversation or 'reply ready'} ({took})", message)
