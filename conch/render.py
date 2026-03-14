"""Terminal rendering helpers."""

from __future__ import annotations

import itertools
import sys
import threading
import time


def highlight(text: str) -> str:
    """Return text unchanged for now."""
    return text


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
            for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                if self._stop.is_set():
                    break
                sys.stderr.write(f"\r\033[36m{frame}\033[0m \033[2m{self.label}\033[0m  ")
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

