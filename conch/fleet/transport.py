"""WorkerTransport: fixed JSON request/response over OpenSSH stdio.

The stable interface (roadmap): one bounded JSON line each way, carried by
``conch-hostctl rpc <worker>`` over an OpenSSH session's stdin/stdout. No
secrets or prompts ever appear in argv — the request travels on stdin, the
response on stdout — and there is no worker network port: the controller
reaches the worker only by opening an SSH session to the host.

The transport is deliberately thin and injectable: it builds the exact
``ssh`` argv from :class:`conch.ssh_control.SSHControlManager` /
:class:`SSHTarget` and shells out through a ``runner`` callable. Tests
substitute a runner backed by a real local worker socket so the wire
contract is exercised without a reachable SSH host; production uses the
default subprocess runner over a restart-safe ``BatchMode=yes`` session.

Connection reuse: the controller ticks every second and makes RPCs for
every in-flight task, so an RPC must not pay a TCP connect + key exchange +
login each time. :class:`SSHConnectionPool` keeps one OpenSSH ControlMaster
per worker host (started lazily, ``ControlPersist`` idle timeout,
re-established when it dies, stopped on shutdown); every RPC then rides the
master as a cheap multiplexed session. A host whose master cannot be
started is backed off, so a dead host costs one bounded connect attempt per
backoff window instead of one per call.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..ssh_control import SSHControlManager, SSHTarget
from ..swarm.protocol import (
    MAX_WIRE_BYTES,
    ProtocolError,
    RpcRequest,
    RpcResponse,
    new_id,
)

#: (returncode, stdout, stderr). A runner never raises for a nonzero rc.
RunnerResult = Tuple[int, bytes, bytes]
Runner = Callable[[list, bytes, float], RunnerResult]

#: (returncode, stderr head). Starts one backgrounded ControlMaster.
Spawner = Callable[[list, float], Tuple[int, bytes]]

#: Idle lifetime of a fleet ControlMaster. The 1 s tick and the 30 s
#: heartbeat sweep keep an enrolled worker's master warm; one nobody uses
#: exits on its own after this long.
FLEET_CONTROL_PERSIST_SECONDS = 300

#: Upper bound on starting one master (ConnectTimeout=10 bounds TCP; this
#: also bounds key exchange and login).
MASTER_START_TIMEOUT = 30.0

#: Wall-clock bound on one streamed artifact transfer.
ARTIFACT_STREAM_TIMEOUT = 900.0

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: ssh stderr fragments meaning "multiplexing is unavailable here" (for
#: example a ControlPath too long for a unix socket) rather than "the host
#: is down": such targets fall back to one direct session per call.
_MUX_UNAVAILABLE = (
    b"unix_listener", b"too long for unix domain socket",
    b"controlsocket", b"muxserver", b"control socket",
)


class TransportError(Exception):
    """The transport failed to deliver a request or parse a reply."""


class ArtifactStreamUnsupported(TransportError):
    """The worker host's hostctl predates ``artifact-get/put --worker``."""


class ArtifactTransferError(Exception):
    """A streamed artifact was refused (size bound, remote error). Not a
    connection failure: the worker itself stays reachable."""


def _subprocess_runner(argv: list, stdin_bytes: bytes,
                       timeout: float) -> RunnerResult:
    try:
        proc = subprocess.run(
            argv, input=stdin_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise TransportError("worker RPC timed out")
    except OSError as exc:
        raise TransportError(f"cannot launch ssh: {exc}")
    return proc.returncode, proc.stdout, proc.stderr


def _subprocess_spawner(argv: list, timeout: float) -> Tuple[int, bytes]:
    # ``ssh -f`` forks the master into the background and that child keeps
    # our stdio: never hand it a pipe (``run`` would then wait for the pipe
    # to close, i.e. for the master to exit). stderr goes to an anonymous
    # temp file so a failed start can still be explained.
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return 255, b"ssh master start timed out"
        except OSError as exc:
            return 255, f"cannot launch ssh: {exc}".encode("utf-8")
        err.seek(0)
        return proc.returncode, err.read(4096)


class SSHConnectionPool:
    """One shared OpenSSH ControlMaster per worker host.

    Thread-safe: the tick thread, the control-socket thread and the
    artifact thread share it. It owns its own :class:`SSHControlManager`,
    so the runtime-dir checks (0700, owned by this user, no symlinks) and
    owned-socket-only cleanup apply unchanged, while the interactive
    ``ssh_remote`` tool keeps its separate manager and semantics.
    """

    def __init__(self, manager: Optional[SSHControlManager] = None, *,
                 spawner: Optional[Spawner] = None,
                 start_timeout: float = MASTER_START_TIMEOUT,
                 backoff_base: float = 2.0, backoff_cap: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.manager = manager or SSHControlManager(
            persist_seconds=FLEET_CONTROL_PERSIST_SECONDS
        )
        self._spawner = spawner or _subprocess_spawner
        self.start_timeout = float(start_timeout)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self._clock = clock
        self._lock = threading.Lock()
        self._target_locks: Dict[str, threading.Lock] = {}
        self._down: Dict[str, Tuple[float, int]] = {}
        self._direct: set = set()
        self._no_stream: set = set()
        self._closed = False
        #: Masters started (= SSH handshakes) and failed starts.
        self.masters_started = 0
        self.master_failures = 0

    def _lock_for(self, identity: str) -> threading.Lock:
        with self._lock:
            lock = self._target_locks.get(identity)
            if lock is None:
                lock = self._target_locks[identity] = threading.Lock()
            return lock

    def ensure(self, target: SSHTarget) -> None:
        """Make sure a live master serves *target*, starting one if needed.

        Raises :class:`TransportError` when the host cannot be reached, and
        keeps raising without spawning ssh until its backoff expires.
        Returns normally (the caller then opens a direct session) only when
        multiplexing itself is unavailable for this target.
        """
        if self._closed:
            raise TransportError("SSH connection pool is closed")
        identity = target.identity
        if identity in self._direct:
            return
        with self._lock_for(identity):
            if self.manager.master_ready(target):
                return
            now = float(self._clock())
            down = self._down.get(identity)
            if down is not None and now < down[0]:
                raise TransportError(
                    f"SSH to {identity} is backing off after a failed"
                    f" connect (retry in {down[0] - now:.0f}s)"
                )
            try:
                argv = self.manager.connect_argv(target, batch=True)
            except OSError as exc:
                raise TransportError(f"cannot prepare SSH master: {exc}")
            rc, err = self._spawner(argv, self.start_timeout)
            if rc == 0 and self.manager.master_ready(target):
                self._down.pop(identity, None)
                self.masters_started += 1
                return
            # Release the reservation (no live socket, so no -O exit spawn).
            self.manager.disconnect(target, timeout=3)
            self.master_failures += 1
            lowered = (err or b"").lower()
            if rc == 0 or any(mark in lowered for mark in _MUX_UNAVAILABLE):
                self._direct.add(identity)
                return
            failures = (down[1] if down else 0) + 1
            delay = min(
                self.backoff_base * (2 ** (failures - 1)), self.backoff_cap
            )
            self._down[identity] = (now + delay, failures)
            detail = (err or b"").decode("utf-8", "replace").strip()
            raise TransportError(
                f"cannot connect to {identity}: "
                + (detail or f"ssh exited {rc}")
            )

    def invalidate(self, target: SSHTarget) -> None:
        """Drop a master that failed a call; the next call starts afresh."""
        with self._lock_for(target.identity):
            self.manager.disconnect(target, timeout=3)

    def stream_supported(self, target: SSHTarget) -> bool:
        return target.identity not in self._no_stream

    def mark_no_stream(self, target: SSHTarget) -> None:
        self._no_stream.add(target.identity)

    def close(self) -> None:
        """Stop every master this pool started (controller shutdown)."""
        self._closed = True
        self.manager.close()


class WorkerTransport:
    """One controller→worker channel: ``ssh host -- conch-hostctl rpc``."""

    def __init__(self, target: SSHTarget, worker: str, *,
                 manager: Optional[SSHControlManager] = None,
                 hostctl: str = "conch-hostctl",
                 rpc_timeout: float = 60.0,
                 runner: Optional[Runner] = None,
                 pool: Optional[SSHConnectionPool] = None,
                 popen: Optional[Callable[..., Any]] = None,
                 artifact_timeout: float = ARTIFACT_STREAM_TIMEOUT):
        self.target = target
        self.worker = str(worker)
        self.pool = pool
        self.manager = manager or (
            pool.manager if pool is not None else SSHControlManager()
        )
        self.hostctl = hostctl
        self.rpc_timeout = float(rpc_timeout)
        self.artifact_timeout = float(artifact_timeout)
        self._runner = runner or _subprocess_runner
        self._popen = popen or subprocess.Popen

    def remote_command(self) -> str:
        # No secrets or prompts in argv: only the fixed relay command and
        # the worker name (validated on the far side).
        return f"{self.hostctl} rpc --worker {self.worker}"

    def argv(self) -> list:
        return self.manager.exec_argv(
            self.target, self.remote_command(), tty=False
        )

    def _prepare(self) -> None:
        if self.pool is not None:
            self.pool.ensure(self.target)

    def _connection_failed(self) -> None:
        if self.pool is not None:
            self.pool.invalidate(self.target)

    def send(self, request: RpcRequest) -> RpcResponse:
        if not isinstance(request, RpcRequest):
            raise TransportError("send expects an RpcRequest")
        line = request.to_json().encode("ascii") + b"\n"
        if len(line) > MAX_WIRE_BYTES:
            raise TransportError("RPC request exceeds the wire bound")
        self._prepare()
        try:
            rc, stdout, stderr = self._runner(
                self.argv(), line, self.rpc_timeout
            )
        except TransportError:
            self._connection_failed()
            raise
        if rc == 255 and not stdout.strip():
            # ssh's own failure code: the master or the link is gone.
            self._connection_failed()
        if len(stdout) > MAX_WIRE_BYTES:
            raise TransportError("RPC response exceeds the wire bound")
        if not stdout.strip():
            detail = stderr.decode("utf-8", "replace").strip()
            raise TransportError(
                f"worker RPC produced no response (rc={rc})"
                + (f": {detail}" if detail else "")
            )
        try:
            response = RpcResponse.from_json(stdout.strip())
        except ProtocolError as exc:
            raise TransportError(f"malformed RPC response: {exc}")
        if response.rpc_id != request.rpc_id:
            raise TransportError(
                "RPC response id did not match the request — refusing"
            )
        return response

    def call(self, op: str, args: Optional[dict] = None) -> RpcResponse:
        return self.send(RpcRequest(
            rpc_id=new_id("rpc"), op=op, args=args or {},
        ))

    # -- streamed artifacts: one hostctl invocation over one session ---------

    def artifact_command(self, verb: str, digest: str) -> str:
        digest = str(digest or "").strip().lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise ArtifactTransferError(f"invalid artifact digest {digest!r}")
        return (
            f"{self.hostctl} artifact-{verb} --worker"
            f" {shlex.quote(self.worker)} --digest {digest}"
        )

    def _stream_argv(self, verb: str, digest: str) -> list:
        if self.pool is not None and not self.pool.stream_supported(
            self.target
        ):
            raise ArtifactStreamUnsupported("hostctl cannot stream")
        command = self.artifact_command(verb, digest)
        self._prepare()
        return self.manager.exec_argv(self.target, command, tty=False)

    def _stream_failure(self, rc: int, stderr: bytes, head: bytes,
                        verb: str) -> Exception:
        if rc == 2 and (b"unrecognized arguments" in stderr
                        or b"invalid choice" in stderr):
            if self.pool is not None:
                self.pool.mark_no_stream(self.target)
            return ArtifactStreamUnsupported(
                "the worker host's hostctl predates streamed artifacts"
            )
        if rc == 255 or rc < 0:
            self._connection_failed()
            detail = stderr.decode("utf-8", "replace").strip()
            return TransportError(
                f"artifact-{verb} stream failed (rc={rc})"
                + (f": {detail}" if detail else "")
            )
        error = ""
        try:
            error = str(json.loads(head.decode("utf-8")).get("error") or "")
        except (ValueError, UnicodeDecodeError, AttributeError):
            pass
        if not error:
            error = stderr.decode("utf-8", "replace").strip()[:300]
        return ArtifactTransferError(
            f"artifact-{verb} failed (rc={rc})"
            + (f": {error}" if error else "")
        )

    def stream_artifact_get(self, digest: str, sink, *, max_bytes: int,
                            cancel: Optional[threading.Event] = None) -> int:
        """Stream one artifact into *sink* (a binary file) over a single
        ``conch-hostctl artifact-get`` session; return the bytes written.

        The caller verifies the digest. More than *max_bytes* kills the
        session and raises :class:`ArtifactTransferError`.
        """
        argv = self._stream_argv("get", digest)
        with tempfile.TemporaryFile() as err:
            try:
                proc = self._popen(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=err,
                )
            except OSError as exc:
                raise TransportError(f"cannot launch ssh: {exc}")
            watchdog = threading.Timer(self.artifact_timeout, proc.kill)
            watchdog.daemon = True
            watchdog.start()
            total = 0
            head = b""
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise ArtifactTransferError("artifact pull cancelled")
                    chunk = proc.stdout.read1(1024 * 1024)
                    if not chunk:
                        break
                    if len(head) < 4096:
                        head += chunk[:4096 - len(head)]
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArtifactTransferError(
                            f"artifact exceeds the {max_bytes}-byte bound"
                        )
                    sink.write(chunk)
                rc = proc.wait(timeout=30)
            finally:
                watchdog.cancel()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
            if rc != 0:
                err.seek(0)
                raise self._stream_failure(rc, err.read(4096), head, "get")
        return total

    def stream_artifact_put(self, digest: str,
                            data: bytes) -> Dict[str, Any]:
        """Push one artifact over a single ``conch-hostctl artifact-put``
        session (the far side verifies the digest before storing)."""
        argv = self._stream_argv("put", digest)
        try:
            proc = self._popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise TransportError(f"cannot launch ssh: {exc}")
        try:
            stdout, stderr = proc.communicate(
                input=data, timeout=self.artifact_timeout
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self._connection_failed()
            raise TransportError("artifact push timed out")
        if proc.returncode != 0:
            raise self._stream_failure(
                proc.returncode, stderr, stdout[:4096], "put"
            )
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ArtifactTransferError("malformed artifact-put reply")
        if not isinstance(result, dict) or not result.get("ok"):
            raise ArtifactTransferError(
                f"artifact push refused: {str(result)[:300]}"
            )
        return {"complete": True,
                "duplicate": bool(result.get("duplicate")),
                "size": int(result.get("size") or 0)}
