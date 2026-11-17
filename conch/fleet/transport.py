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
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional, Tuple

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


class TransportError(Exception):
    """The transport failed to deliver a request or parse a reply."""


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


class WorkerTransport:
    """One controller→worker channel: ``ssh host -- conch-hostctl rpc``."""

    def __init__(self, target: SSHTarget, worker: str, *,
                 manager: Optional[SSHControlManager] = None,
                 hostctl: str = "conch-hostctl",
                 rpc_timeout: float = 60.0,
                 runner: Optional[Runner] = None):
        self.target = target
        self.worker = str(worker)
        self.manager = manager or SSHControlManager()
        self.hostctl = hostctl
        self.rpc_timeout = float(rpc_timeout)
        self._runner = runner or _subprocess_runner

    def remote_command(self) -> str:
        # No secrets or prompts in argv: only the fixed relay command and
        # the worker name (validated on the far side).
        return f"{self.hostctl} rpc --worker {self.worker}"

    def argv(self) -> list:
        return self.manager.exec_argv(
            self.target, self.remote_command(), tty=False
        )

    def send(self, request: RpcRequest) -> RpcResponse:
        if not isinstance(request, RpcRequest):
            raise TransportError("send expects an RpcRequest")
        line = request.to_json().encode("ascii") + b"\n"
        if len(line) > MAX_WIRE_BYTES:
            raise TransportError("RPC request exceeds the wire bound")
        rc, stdout, stderr = self._runner(
            self.argv(), line, self.rpc_timeout
        )
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
