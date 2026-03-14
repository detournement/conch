"""Lightweight Composio helpers."""

from __future__ import annotations

import os
from typing import List, Tuple


_KNOWN_APPS: List[Tuple[str, str]] = [
    ("gmail", "Google Mail"),
    ("github", "GitHub"),
    ("slack", "Slack"),
    ("serpapi", "Web search"),
]


def is_available() -> bool:
    return bool(os.environ.get("COMPOSIO_API_KEY", "").strip())


def list_apps() -> List[Tuple[str, str]]:
    return list(_KNOWN_APPS)


def connect(app_slug: str) -> tuple[bool, str]:
    if not is_available():
        return False, "COMPOSIO_API_KEY not set"
    return True, f"{app_slug} connection requested. Reload tools if new tools were added."

