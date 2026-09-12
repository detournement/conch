"""Direct, non-recording terminal handoff for credential-aware programs."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import termios
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any


@dataclass
class TerminalHandoffPolicy:
    """Capabilities required for a direct terminal handoff.

    ``local_session`` is an authority boundary, not a presentation hint:
    scheduled and channel-driven sessions must set it to ``False``.
    """

    local_session: bool = True
    input_fn: Any = None
    handoff_context: Callable[[], AbstractContextManager[Any]] | None = None
    tty_check: Callable[[], bool] | None = None


@dataclass(frozen=True)
class TerminalRunResult:
    approved: bool
    returncode: int | None = None
    timed_out: bool = False
    interrupted: bool = False
    error: str = ""


def stdio_is_interactive() -> bool:
    """Return True only when input and output are attached to real terminals."""

    try:
        return bool(
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and sys.stderr.isatty()
        )
    except (AttributeError, OSError, ValueError):
        return False


def discard_pending_terminal_input() -> None:
    """Drop text pasted before the handoff program has disabled echo."""

    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            termios.tcflush(fd, termios.TCIFLUSH)
    except (AttributeError, OSError, termios.error, ValueError):
        return


@contextlib.contextmanager
def preserve_terminal_state():
    """Restore terminal flags even when a handed-off program fails or aborts."""

    saved: list[tuple[int, list]] = []
    seen = set()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.flush()
            fd = stream.fileno()
            if fd in seen or not os.isatty(fd):
                continue
            seen.add(fd)
            saved.append((fd, termios.tcgetattr(fd)))
        except (AttributeError, OSError, termios.error, ValueError):
            continue
    try:
        yield
    finally:
        for fd, attributes in reversed(saved):
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, attributes)
            except (OSError, termios.error, ValueError):
                pass
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass


@contextlib.contextmanager
def restore_after_termination_signals():
    """Turn HUP/TERM into orderly unwinding while a child owns the TTY."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    for signum in (
        getattr(signal, "SIGHUP", None),
        signal.SIGTERM,
        getattr(signal, "SIGQUIT", None),
    ):
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, terminate)
        except (OSError, RuntimeError, ValueError):
            previous.pop(signum, None)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError):
                pass


class DirectTerminalRunner:
    """Run argv with inherited terminal file descriptors and no transcript.

    Conch reads only the explicit yes/no confirmation. Once execution starts,
    the child inherits the real controlling terminal; stdin, stdout, and
    stderr are never piped through Python.
    """

    def __init__(self, policy: TerminalHandoffPolicy | None = None):
        self.policy = policy or TerminalHandoffPolicy()

    def set_policy(self, policy: TerminalHandoffPolicy) -> None:
        self.policy = policy

    def available(self) -> bool:
        check = self.policy.tty_check or stdio_is_interactive
        return bool(self.policy.local_session and check())

    @staticmethod
    def _stop_child(proc) -> None:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=1)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.kill()
            proc.wait()
        except OSError:
            pass

    def run(
        self,
        argv: Sequence[str],
        *,
        description: str,
        timeout: int = 0,
    ) -> TerminalRunResult:
        """Confirm, then execute directly on the user's terminal.

        No child output or input is returned. The caller receives only process
        status suitable for a non-secret tool result.
        """

        if not self.policy.local_session:
            return TerminalRunResult(
                approved=False,
                error=(
                    "interactive terminal handoff is disabled outside the "
                    "local foreground session"
                ),
            )
        check = self.policy.tty_check or stdio_is_interactive
        if not check():
            return TerminalRunResult(
                approved=False,
                error="interactive terminal handoff requires a real local TTY",
            )

        context_factory = self.policy.handoff_context
        outer = (
            context_factory()
            if context_factory is not None
            else contextlib.nullcontext()
        )
        with outer, preserve_terminal_state(), restore_after_termination_signals():
            input_fn = self.policy.input_fn or input
            try:
                answer = input_fn(
                    f"  \033[1;33m{description} [y/N]\033[0m "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                return TerminalRunResult(approved=False)

            discard_pending_terminal_input()
            print(
                "\n\033[1;36m[Conch terminal handoff]\033[0m\n"
                "  Input and output now go directly to the program.\n"
                "  Conch is not recording this interaction. Finish the "
                "program or press Ctrl+C to return.\n",
                flush=True,
            )
            proc = None
            result: TerminalRunResult
            try:
                # Deliberately omit stdin/stdout/stderr and env: the process
                # inherits the controlling terminal and current environment.
                # In particular, no credential is passed by Conch.
                proc = subprocess.Popen(list(argv), close_fds=True)
                try:
                    returncode = proc.wait(
                        timeout=timeout if timeout and timeout > 0 else None
                    )
                    result = TerminalRunResult(
                        approved=True, returncode=returncode
                    )
                except subprocess.TimeoutExpired:
                    self._stop_child(proc)
                    result = TerminalRunResult(
                        approved=True,
                        returncode=proc.returncode,
                        timed_out=True,
                    )
                except KeyboardInterrupt:
                    self._stop_child(proc)
                    result = TerminalRunResult(
                        approved=True,
                        returncode=proc.returncode,
                        interrupted=True,
                    )
                except BaseException:
                    self._stop_child(proc)
                    raise
            except OSError as exc:
                result = TerminalRunResult(
                    approved=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                print(
                    "\n\033[1;36m[Conch resumed]\033[0m "
                    "Terminal input/output was not captured.\n",
                    flush=True,
                )
            return result
