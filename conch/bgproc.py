"""Background processes for ``local_shell`` (dev servers, watchers, long builds).

``local_shell`` with ``background: true`` goes through the exact approval
path of a foreground command, then hands the approved command to the
session's :class:`ProcessRegistry` instead of blocking on it. Each process
runs in its own session/process group on a PTY (stdin is /dev/null, as in
the foreground path); a daemon reader thread feeds its output through the
same incremental UTF-8 decoder and terminal-control sanitizer as foreground
capture, into a bounded ring buffer. The model reads new output with
``process_output``, stops a group with ``process_kill`` (SIGTERM, then
SIGKILL after a grace period) and lists what it started with
``process_list``.

Lifecycle: at most :data:`MAX_RUNNING` processes run per registry; the
owning session kills them all on close, and an ``atexit`` hook covers
every registry still alive at interpreter exit.

Threading: only the reader thread ever reads or closes its PTY master fd
(closing an fd another thread is blocked on does not interrupt the read on
Linux), so stopping a process means signalling the group and setting an
event the reader polls between bounded ``select`` waits.
"""

from __future__ import annotations

import atexit
import codecs
import errno
import os
import re
import select
import signal
import subprocess
import threading
import time
import weakref
from typing import Dict, List, Optional, Tuple

#: Concurrent running background processes per session.
MAX_RUNNING = 8
#: Finished records kept for process_list/process_output before pruning.
MAX_RECORDS = 32
#: Characters of sanitized output kept per process (about 1-2 MB of text).
RING_CHARS = 1_000_000
#: How long a background start waits to return the first output.
STARTUP_WAIT = 1.5
#: SIGTERM -> SIGKILL grace period for process_kill and session close.
KILL_GRACE = 3.0
#: Upper bound for process_output's ``wait`` argument (seconds).
MAX_WAIT = 300

PROCESS_TOOL_NAMES = ("process_output", "process_kill", "process_list")

REDACTION_MARKER = "[credential redacted]"


class RegistryFull(RuntimeError):
    """Refused: the per-session cap on running background processes."""


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class BackgroundProcess:
    """One detached command: its process group, reader thread and ring buffer."""

    def __init__(self, proc_id: str, command: str, proc: subprocess.Popen,
                 master_fd: int, ring_chars: int = RING_CHARS):
        from .tooling import _CapturedTerminalText

        self.id = proc_id
        self.command = command
        self.proc = proc
        self.pid = proc.pid
        # start_new_session: the child leads its own session and group.
        self.pgid = proc.pid
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.ended: Optional[float] = None
        self.killed = False
        self._fd = master_fd
        self._ring_chars = max(1024, int(ring_chars))
        self._buf = ""
        self._total = 0
        self._cursor = 0
        self._pending_cr = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._eof = threading.Event()
        self._sanitizer = _CapturedTerminalText()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._thread = threading.Thread(
            target=self._reader, name=f"conch-bg-{proc_id}", daemon=True,
        )
        self._thread.start()

    # -- capture (reader thread only) ---------------------------------------

    def _reader(self) -> None:
        from .tooling import _PTY_READ_SIZE

        fd = self._fd
        try:
            while not self._stop.is_set():
                if not self._read_once(fd, _PTY_READ_SIZE, 0.25):
                    break
            else:
                # Stopped (killed): keep whatever is already buffered.
                while self._read_once(fd, _PTY_READ_SIZE, 0):
                    pass
            tail = self._sanitizer.feed(self._decoder.decode(b"", final=True))
            self._append(tail + self._sanitizer.finish(), final=True)
        except Exception:  # noqa: BLE001 - never kill the interpreter thread noisily
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self._eof.set()

    def _read_once(self, fd: int, size: int, timeout: float) -> bool:
        """Read one chunk; False on EOF/error (or nothing ready at timeout 0)."""
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except InterruptedError:
            return True
        except (OSError, ValueError):
            return False
        if not ready:
            return timeout > 0
        try:
            data = os.read(fd, size)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                return True
            return False  # EIO: every slave end is closed
        if not data:
            return False
        self._append(self._sanitizer.feed(self._decoder.decode(data)))
        return True

    def _append(self, text: str, final: bool = False) -> None:
        text = self._pending_cr + text
        self._pending_cr = ""
        if text.endswith("\r") and not final:
            # A CRLF split across reads must not become two line breaks.
            self._pending_cr, text = "\r", text[:-1]
        if not text:
            return
        # Same folding as foreground capture: PTY CRLF -> \n, then a bare
        # \r (progress bars rewriting a line) becomes a line break.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        with self._lock:
            self._buf += text
            self._total += len(text)
            if len(self._buf) > self._ring_chars:
                self._buf = self._buf[-self._ring_chars:]

    # -- status ---------------------------------------------------------------

    def returncode(self) -> Optional[int]:
        code = self.proc.poll()
        if code is not None and self.ended is None:
            self.ended = time.monotonic()
        return code

    def running(self) -> bool:
        return self.returncode() is None

    def elapsed(self) -> float:
        self.returncode()
        end = self.ended if self.ended is not None else time.monotonic()
        return max(0.0, end - self.started)

    def status_text(self) -> str:
        code = self.returncode()
        if code is None:
            return "running"
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = f"signal {-code}"
            how = "killed" if self.killed else "terminated"
            return f"{how} by {name}"
        return f"exited with code {code}"

    @property
    def total_chars(self) -> int:
        with self._lock:
            return self._total

    # -- reads ----------------------------------------------------------------

    def read(self, since: Optional[int] = None) -> Tuple[str, int, int, int]:
        """New output from *since* (default: where the last read stopped).

        Returns ``(text, start, end, dropped)``: absolute char offsets of the
        returned slice and how many chars before it the ring buffer had
        already discarded. Advances the read cursor to *end*.
        """
        with self._lock:
            buf_start = self._total - len(self._buf)
            if since is None:
                since = self._cursor
            since = max(0, min(int(since), self._total))
            begin = max(since, buf_start)
            dropped = begin - since
            text = self._buf[begin - buf_start:]
            end = self._total
            self._cursor = end
        return text, begin, end, dropped

    def wait_exit(self, timeout: float) -> bool:
        """Block up to *timeout* s for the process to exit; True if it did.
        Also gives the reader a moment to drain the final output."""
        deadline = time.monotonic() + max(0.0, timeout)
        while self.returncode() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._eof.wait(min(0.05, remaining)):
                # Output closed; the exit status follows promptly.
                try:
                    self.proc.wait(timeout=max(0.0, min(0.5, remaining)))
                except subprocess.TimeoutExpired:
                    pass
        self._eof.wait(0.5)
        return True

    # -- termination ------------------------------------------------------------

    def terminate(self) -> None:
        """Send SIGTERM to the whole group (first half of kill)."""
        self.killed = True
        _signal_group(self.pgid, signal.SIGTERM)

    def group_alive(self) -> bool:
        self.returncode()  # reap the leader so it is not counted as a zombie
        return _group_alive(self.pgid)

    def force_kill(self) -> None:
        self.killed = True
        _signal_group(self.pgid, signal.SIGKILL)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def finish_reader(self, timeout: float = 1.0) -> None:
        """Tell the reader to stop (a detached grandchild may hold the PTY
        open forever) and give it *timeout* s to drain and close its fd."""
        self._stop.set()
        self._eof.wait(timeout)


def kill_processes(procs: List[BackgroundProcess],
                   grace: float = KILL_GRACE) -> None:
    """SIGTERM every group, wait up to *grace* s, SIGKILL what remains."""
    live = [p for p in procs if p.group_alive()]
    for proc in live:
        proc.terminate()
    deadline = time.monotonic() + max(0.0, grace)
    while live and time.monotonic() < deadline:
        live = [p for p in live if p.group_alive()]
        if live:
            time.sleep(0.05)
    for proc in live:
        proc.force_kill()
    for proc in procs:
        proc.returncode()
        proc.finish_reader(0.5)


class ProcessRegistry:
    """The background processes one session started (shared with delegated
    subagents, which use the parent's local_shell client)."""

    def __init__(self, max_running: int = MAX_RUNNING,
                 ring_chars: int = RING_CHARS, kill_grace: float = KILL_GRACE):
        self.max_running = max(1, int(max_running))
        self.ring_chars = ring_chars
        self.kill_grace = kill_grace
        self._procs: Dict[str, BackgroundProcess] = {}
        self._counter = 0
        self._lock = threading.Lock()
        _REGISTRIES.add(self)

    def running(self) -> List[BackgroundProcess]:
        return [p for p in self.list() if p.running()]

    def list(self) -> List[BackgroundProcess]:
        with self._lock:
            return list(self._procs.values())

    def get(self, proc_id: str) -> Optional[BackgroundProcess]:
        key = str(proc_id or "").strip()
        with self._lock:
            proc = self._procs.get(key)
            if proc is None and key.isdigit():
                proc = self._procs.get(f"p{key}")
        return proc

    def full_message(self) -> str:
        return (
            f"Refused: {self.max_running} background processes are already "
            "running (the per-session cap). Stop one with process_kill first."
        )

    def at_capacity(self) -> bool:
        return len(self.running()) >= self.max_running

    def start(self, command, *, shell: bool = True,
              cwd: Optional[str] = None) -> BackgroundProcess:
        import pty

        with self._lock:
            running = [p for p in self._procs.values() if p.running()]
            if len(running) >= self.max_running:
                raise RegistryFull(self.full_message())
            self._prune_locked()
            master_fd, slave_fd = pty.openpty()
            try:
                proc = subprocess.Popen(
                    command, shell=shell, cwd=cwd,
                    stdin=subprocess.DEVNULL, stdout=slave_fd, stderr=slave_fd,
                    close_fds=True, start_new_session=True,
                )
            except BaseException:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            self._counter += 1
            proc_id = f"p{self._counter}"
            label = command if isinstance(command, str) else " ".join(map(str, command))
            record = BackgroundProcess(proc_id, label, proc, master_fd,
                                       ring_chars=self.ring_chars)
            self._procs[proc_id] = record
            return record

    def _prune_locked(self) -> None:
        excess = len(self._procs) - (MAX_RECORDS - 1)
        if excess <= 0:
            return
        for key in [k for k, p in self._procs.items() if not p.running()][:excess]:
            del self._procs[key]

    def kill(self, proc_id: str, grace: Optional[float] = None) -> Optional[BackgroundProcess]:
        proc = self.get(proc_id)
        if proc is None:
            return None
        kill_processes([proc], self.kill_grace if grace is None else grace)
        return proc

    def kill_all(self, grace: Optional[float] = None) -> List[BackgroundProcess]:
        """Stop every process still running; returns the ones that were."""
        live = [p for p in self.list() if p.group_alive()]
        if live:
            kill_processes(live, self.kill_grace if grace is None else grace)
        for proc in self.list():
            proc.finish_reader(0.2)
        return live


_REGISTRIES: "weakref.WeakSet[ProcessRegistry]" = weakref.WeakSet()


def _kill_all_registries() -> None:
    for registry in list(_REGISTRIES):
        try:
            registry.kill_all(grace=1.0)
        except Exception:  # noqa: BLE001 - interpreter is exiting
            pass


atexit.register(_kill_all_registries)


# ---------------------------------------------------------------------------
# Model-facing formatting
# ---------------------------------------------------------------------------

def _short(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_line(proc: BackgroundProcess) -> str:
    from .secretguard import redact_credentials

    command = redact_credentials(proc.command, REDACTION_MARKER)[0]
    return (
        f"{proc.id} (pid {proc.pid}) {proc.status_text()}, "
        f"{proc.elapsed():.1f}s elapsed: {_short(command)}"
    )


def format_output(proc: BackgroundProcess, budget: int,
                  since: Optional[int] = None, started: bool = False) -> str:
    """The process_output result: status header, new output, next cursor.

    New output gets the foreground treatment: blank-line runs collapsed,
    credential-shaped values redacted, then head+tail truncated to the
    tool result budget.
    """
    from .runtime import truncate_middle
    from .secretguard import redact_credentials

    text, begin, end, dropped = proc.read(since)
    header = ("Started background process " if started else "Process ") + status_line(proc)
    parts = [header]
    if dropped:
        parts.append(
            f"[{dropped:,} earlier chars were dropped from the "
            f"{proc._ring_chars:,}-char output buffer]"
        )
    body = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    labels: List[str] = []
    if body.strip():
        body, labels = redact_credentials(body, REDACTION_MARKER)
        room = max(200, int(budget) - 400)
        if len(body) > room:
            body = truncate_middle(body, room)
        parts.append(body)
    else:
        parts.append("(no new output)")
    if labels:
        parts.append(f"[conch redacted credential-shaped values: {', '.join(labels)}]")
    footer = f"[output chars {begin:,}-{end:,}; process_output(id=\"{proc.id}\") continues from {end:,}"
    if proc.running():
        footer += "; still running: process_kill it when done]"
    else:
        footer += "]"
    parts.append(footer)
    return "\n".join(parts)


def format_list(procs: List[BackgroundProcess]) -> str:
    if not procs:
        return "No background processes in this session."
    lines = [f"Background processes ({sum(p.running() for p in procs)} running):"]
    for proc in procs:
        lines.append(f"- {status_line(proc)} [{proc.total_chars:,} output chars]")
    return "\n".join(lines)
