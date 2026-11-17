"""What the session's subagents are doing: fan-out workers and delegate_task
runs, each recorded when it starts and when it ends, for the /agents panel.

Process-wide and thread-safe; fan-out workers run on pool threads and the
panel reads from the shell's thread. Only the most recent finished runs are
kept.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from typing import Deque, List, Optional

KINDS = ("fan-out", "delegate")
STATUSES = ("done", "failed", "cancelled")
KEEP_FINISHED = 30
LABEL_MAX = 100


@dataclass
class Run:
    token: int
    kind: str  # fan-out | delegate
    label: str  # first line of the task
    model: str  # provider/model (or the preference, for delegate)
    started: float  # time.time()
    ended: Optional[float] = None
    status: str = "running"  # running | done | failed | cancelled
    summary: str = ""

    @property
    def running(self) -> bool:
        return self.ended is None

    def seconds(self, now: Optional[float] = None) -> float:
        """Elapsed so far while running, else the duration."""
        end = self.ended if self.ended is not None else (time.time() if now is None else now)
        return max(0.0, end - self.started)


_lock = threading.Lock()
_tokens = itertools.count(1)
_running: "OrderedDict[int, Run]" = OrderedDict()
_finished: Deque[Run] = deque(maxlen=KEEP_FINISHED)


def first_line(text: str, limit: int = LABEL_MAX) -> str:
    """The first non-blank line of *text*, cut to *limit* characters."""
    for line in str(text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= limit else line[:limit - 1] + "…"
    return ""


def start(kind: str, label: str, model: str = "") -> int:
    """Record a run that has just started; the token :func:`finish` takes."""
    with _lock:
        token = next(_tokens)
        _running[token] = Run(token, kind, first_line(label), str(model or ""), time.time())
    return token


def finish(token: int, status: str, summary: str = "") -> None:
    """Record how a run ended (done, failed, or cancelled). Unknown or
    already-finished tokens are ignored."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with _lock:
        run = _running.pop(token, None)
        if run is None:
            return
        run.ended = time.time()
        run.status = status
        run.summary = first_line(summary, 200)
        _finished.append(run)


def snapshot() -> List[Run]:
    """Copies of every run: running ones first (oldest first), then the
    finished ones (most recent first)."""
    with _lock:
        running = [replace(run) for run in _running.values()]
        finished = [replace(run) for run in reversed(_finished)]
    return running + finished


def clear() -> None:
    """Forget every run (tests)."""
    with _lock:
        _running.clear()
        _finished.clear()
