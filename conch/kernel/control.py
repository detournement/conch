"""Versioned JSON control protocol over a permission-protected unix socket.

The daemon listens on a socket inside a 0700 directory (XDG runtime dir
when set, else ``<state>/conch/run``); the socket file itself is 0600.
Directory and file permissions are the authorization boundary — the daemon
runs unprivileged under the logged-in user and never accepts inbound
network connections.

Wire format: exactly one JSON object per line in each direction.

    → {"v": 1, "op": "missions.list", "args": {...}}
    ← {"v": 1, "ok": true, "result": ...}
    ← {"v": 1, "ok": false, "error": "..."}

Any protocol-version mismatch fails closed on both sides. Responses never
carry secret bytes (kernel rows never contain them in the first place).
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

from .store import default_state_dir

CONTROL_PROTOCOL_VERSION = 1

#: Hard bound on one control request/response line.
MAX_CONTROL_LINE_BYTES = 512 * 1024

DEFAULT_TIMEOUT = 10.0


class ControlError(Exception):
    """A control request failed (transport, protocol, or daemon-side)."""


def runtime_dir() -> Path:
    """0700 directory for the control socket."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        return Path(xdg) / "conch"
    return default_state_dir() / "run"


def control_socket_path() -> Path:
    return runtime_dir() / "edge.sock"


def ensure_runtime_dir() -> Path:
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _recv_line(sock: socket.socket) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_CONTROL_LINE_BYTES:
            raise ControlError("control response exceeds size bound")
        if chunk.endswith(b"\n"):
            break
    return b"".join(chunks)


def request(op: str, args: Optional[Dict[str, Any]] = None, *,
            socket_path: Optional[Path] = None,
            timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Send one control request; return the result or raise ControlError."""
    path = Path(socket_path) if socket_path else control_socket_path()
    payload = json.dumps({
        "v": CONTROL_PROTOCOL_VERSION, "op": str(op), "args": args or {},
    }) + "\n"
    if len(payload.encode("utf-8")) > MAX_CONTROL_LINE_BYTES:
        raise ControlError("control request exceeds size bound")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(path))
        except OSError as exc:
            raise ControlError(f"daemon socket unavailable: {exc}")
        try:
            sock.sendall(payload.encode("utf-8"))
            raw = _recv_line(sock)
        except OSError as exc:
            raise ControlError(f"daemon connection failed: {exc}")
    finally:
        sock.close()
    if not raw:
        raise ControlError("daemon closed the connection without replying")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ControlError(f"malformed daemon response: {exc}")
    if not isinstance(response, dict):
        raise ControlError("malformed daemon response: not an object")
    if response.get("v") != CONTROL_PROTOCOL_VERSION:
        raise ControlError(
            f"unsupported control protocol version {response.get('v')!r}"
            f" (supported: {CONTROL_PROTOCOL_VERSION}) — failing closed"
        )
    if not response.get("ok"):
        raise ControlError(str(response.get("error") or "daemon error"))
    return response.get("result")


def daemon_alive(socket_path: Optional[Path] = None,
                 timeout: float = 2.0) -> bool:
    """True when a daemon answers a status ping on the control socket."""
    try:
        result = request(
            "status", socket_path=socket_path, timeout=timeout
        )
        return isinstance(result, dict)
    except ControlError:
        return False
