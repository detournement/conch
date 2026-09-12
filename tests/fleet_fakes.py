"""Shared fleet test doubles: a fake SSH transport that is byte-identical
to production RPC.

The controller talks to a worker as ``conch-hostctl rpc <worker>`` relaying
one bounded JSON line over SSH stdio. The fake here spawns the *real*
worker supervisor as a local subprocess and connects to its *real* unix
socket, so the RPC / lease / fencing / receipt logic under test is exactly
the production path — only the SSH hop is replaced by a local pipe.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from conch.swarm.protocol import MAX_WIRE_BYTES

ROOT = Path(__file__).resolve().parents[1]


class LocalWorkerProcess:
    """Spawn the real conch-worker supervisor for a worker home."""

    def __init__(self, home: Path, env_extra: Optional[dict] = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self._env_extra = dict(env_extra or {})
        self._log = None

    def start(self) -> "LocalWorkerProcess":
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        env.update(self._env_extra)
        self._log = open(self.home / "supervisor-stdout.log", "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "conch.fleet.worker",
             "--home", str(self.home)],
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
            env=env, start_new_session=True,
        )
        self._wait_for_socket()
        return self

    def _wait_for_socket(self, timeout: float = 20.0) -> None:
        socket_path = self.home / "run" / "supervisor.sock"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if socket_path.exists() and self.rpc_raw(
                {"v": 1}
            ) is not None:
                return
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "worker supervisor exited early; see "
                    f"{self.home / 'supervisor-stdout.log'}"
                )
            time.sleep(0.1)
        raise RuntimeError("worker supervisor socket never appeared")

    def rpc_raw(self, request_obj) -> Optional[bytes]:
        """Send a raw JSON object; return the raw reply bytes (or None)."""
        socket_path = self.home / "run" / "supervisor.sock"
        if not socket_path.exists():
            return None
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10.0)
        try:
            client.connect(str(socket_path))
            client.sendall(
                json.dumps(request_obj).encode("utf-8") + b"\n"
            )
            chunks = []
            total = 0
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_WIRE_BYTES or chunk.endswith(b"\n"):
                    break
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            client.close()

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    self.proc.kill()
        self.proc = None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def kill9(self) -> None:
        """Simulate a hard crash (fault injection)."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                self.proc.kill()
            self.proc.wait(timeout=10)


class FakeSSHWorkerTransport:
    """WorkerTransport-shaped client over a LocalWorkerProcess socket.

    The production transport is ``conch-hostctl rpc`` over SSH stdio; this
    connects to the same supervisor socket directly so the wire protocol
    and supervisor logic are exercised unchanged.
    """

    def __init__(self, worker: LocalWorkerProcess):
        self.worker = worker
        self.calls = 0

    def send(self, request):
        from conch.swarm.protocol import RpcRequest, RpcResponse

        if isinstance(request, RpcRequest):
            request_obj = request.to_dict()
        else:
            request_obj = request
        self.calls += 1
        raw = self.worker.rpc_raw(request_obj)
        if not raw:
            raise ConnectionError("worker unreachable over fake transport")
        return RpcResponse.from_json(raw)
