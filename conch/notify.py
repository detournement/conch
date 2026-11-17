"""Desktop notifications for long turns.

A turn that runs at least ``notify_after_seconds`` (default 30) posts a
desktop notification when it finishes, so a user who switched to another
window knows the reply is ready. macOS uses ``osascript``; Linux uses
``notify-send``. Anywhere else, or when the tool is missing, nothing
happens. Only interactive sessions notify; ``notify_after_seconds=0`` (or
``off``) turns it off.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import List, Optional

DEFAULT_AFTER_SECONDS = 30.0
_MAX_MESSAGE_CHARS = 180


def notify_after_seconds(config: Optional[dict]) -> float:
    """The configured threshold in seconds; 0 means never notify. An
    unparseable value keeps the default rather than silently disabling."""
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


def notification_command(title: str, message: str,
                         platform: Optional[str] = None) -> Optional[List[str]]:
    """The argv that shows the notification on *platform*, or None.

    Text travels as argv, never spliced into the AppleScript source, so a
    reply cannot inject script."""
    platform = platform or sys.platform
    title = _plain(title, 80)
    message = _plain(message, _MAX_MESSAGE_CHARS)
    if platform == "darwin" and shutil.which("osascript"):
        return ["osascript",
                "-e", "on run argv",
                "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run",
                title, message]
    if platform.startswith("linux") and shutil.which("notify-send"):
        return ["notify-send", "--app-name=Conch", title, message]
    return None


def send(title: str, message: str) -> bool:
    """Post a notification without waiting for it; True when one was sent."""
    command = notification_command(title, message)
    if command is None:
        return False
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except OSError:
        return False
    return True


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
