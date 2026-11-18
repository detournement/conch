"""Cooperative, session-local pause boundaries and fan-out pause transport.

An admitted request/tool may finish. Nothing is suspended mid-operation. The
optional private IPC file exists only for this process lifetime; children treat
its removal as cancellation, never as permission to continue.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import tempfile
import threading
import time


class AgentsCancelled(KeyboardInterrupt):
    """A closed session or stopped worker must unwind, not retry inference."""


class PauseController:
    """One session tree's brake, shared explicitly with descendants."""

    POLL_SECONDS = 0.05

    def __init__(self, path=None):
        self._condition = threading.Condition()
        self._paused_at = None
        self._paused_seconds = 0.0
        self._closed = False
        self._path = path
        self._remote = path is not None
        self._directory = None

    def _state(self):
        if self._remote:
            try:
                with open(self._path, encoding="utf-8") as stream:
                    state = json.load(stream)
                return state["paused_at"], state["paused_seconds"], state["closed"]
            except (OSError, ValueError, KeyError, TypeError):
                return None, 0.0, True
        return self._paused_at, self._paused_seconds, self._closed

    @property
    def paused(self):
        with self._condition:
            paused_at, _, closed = self._state()
            return paused_at is not None and not closed

    @property
    def closed(self):
        with self._condition:
            return self._state()[2]

    def _publish(self):
        if self._path is None or self._remote:
            return
        temporary = self._path + ".new"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump({"paused_at": self._paused_at,
                       "paused_seconds": self._paused_seconds,
                       "closed": self._closed}, stream)
        os.replace(temporary, self._path)

    def export_path(self):
        """Lazily create the local IPC transport before spawning any workers."""
        with self._condition:
            if self._closed:
                raise AgentsCancelled("session closed")
            if self._path is None:
                self._directory = tempfile.TemporaryDirectory(prefix="conch-ebrake-")
                self._path = os.path.join(self._directory.name, "state.json")
                self._publish()
            return self._path

    def pause(self):
        with self._condition:
            if not self._remote and not self._closed and self._paused_at is None:
                self._paused_at = time.monotonic()
                self._publish()
                self._condition.notify_all()

    def resume(self):
        with self._condition:
            if not self._remote and not self._closed and self._paused_at is not None:
                self._paused_seconds += time.monotonic() - self._paused_at
                self._paused_at = None
                self._publish()
                self._condition.notify_all()

    def active_time(self):
        """Monotonic clock excluding all paused intervals, including this one."""
        with self._condition:
            paused_at, paused_seconds, _ = self._state()
            now = time.monotonic()
            return (paused_at if paused_at is not None else now) - paused_seconds

    def wait(self, cancelled=(), *, on_pause=None):
        """Wait without consuming work; cancellation always wins over resume."""
        with self._condition:
            while True:
                paused_at, _, closed = self._state()
                if self._closed or closed or any(event.is_set() for event in cancelled):
                    raise AgentsCancelled("agent work cancelled")
                if paused_at is None:
                    return
                if on_pause is not None:
                    on_pause()
                # Polling also observes remote state and caller-owned stop events.
                self._condition.wait(self.POLL_SECONDS)

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
            if self._directory is not None:
                try:
                    self._publish()
                except OSError:
                    pass
                try:
                    self._directory.cleanup()
                except OSError:
                    pass
                self._directory = None


# ContextVars propagate synchronous nested skill/chat_turn calls, but unrelated
# sessions always install their own controller. Thread/process workers opt in.
_SCOPE = contextvars.ContextVar("conch_agent_pause", default=None)
_EXECUTION_LOCK = threading.Lock()
_EXECUTION = threading.local()


def current_controller():
    scope = _SCOPE.get()
    return scope[0] if scope is not None else None


@contextlib.contextmanager
def agent_scope(controller, closed_event):
    inherited = _SCOPE.get()
    events = inherited[1] if inherited and inherited[0] is controller else ()
    token = _SCOPE.set((controller, events + (closed_event,)))
    try:
        checkpoint()
        yield
    finally:
        _SCOPE.reset(token)


def _release_execution():
    if getattr(_EXECUTION, "held", False):
        _EXECUTION.held = False
        _EXECUTION_LOCK.release()


def _acquire_execution():
    while not getattr(_EXECUTION, "held", False):
        scope = _SCOPE.get()
        if scope is not None:
            scope[0].wait(scope[1])
        if _EXECUTION_LOCK.acquire(timeout=PauseController.POLL_SECONDS):
            _EXECUTION.held = True
            # Pause may have arrived while waiting for another session's turn.
            if scope is not None and scope[0].paused:
                _release_execution()
                continue
            if scope is not None:
                try:
                    scope[0].wait(scope[1], on_pause=_release_execution)
                except BaseException:
                    _release_execution()
                    raise


def checkpoint(*, cancelled=()):
    """Park at a safe boundary, releasing all nested execution-lock scopes."""
    scope = _SCOPE.get()
    if scope is None:
        return
    controller, events = scope
    events = events + tuple(cancelled)
    # Release before waiting (also on close), so a parked turn cannot strand
    # independent sessions. No private RLock internals or lock-depth guessing.
    if controller.paused or controller.closed or any(event.is_set() for event in events):
        _release_execution()
    controller.wait(events, on_pause=_release_execution)
    if getattr(_EXECUTION, "depth", 0):
        _acquire_execution()


@contextlib.contextmanager
def serialized_execution():
    """Reentrant turn serialization whose lock is relinquished while paused."""
    _EXECUTION.depth = getattr(_EXECUTION, "depth", 0) + 1
    try:
        checkpoint()
        _acquire_execution()
        yield
    finally:
        _EXECUTION.depth -= 1
        if not _EXECUTION.depth:
            _release_execution()
