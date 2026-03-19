"""Conversation browser helpers."""

from __future__ import annotations


def browse_conversations(conv_mgr, current_id: str = ""):
    """Fallback text-only browser hook.

    The richer curses browser can be added later; for now we preserve the API.
    """
    print("\n  \033[2mVisual browser is unavailable in this build. Use /convos and /switch.\033[0m\n")
    return current_id or None

