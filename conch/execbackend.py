"""Pluggable execution backends: run approved shell commands in a sandbox.

The permission model is untouched — ``LocalShellClient`` decides *whether*
a command may run exactly as before (prompts, safe_auto, destructive
gates); a backend only changes *where* the approved command executes:

- ``local``  — the default subprocess/PTY path (no backend object at all);
- ``docker`` — an ephemeral local container per session; commands become
  ``docker exec`` argv and reuse the normal PTY streaming/capture path;
- ``e2b``    — an E2B cloud sandbox (https://e2b.dev) over a stdlib-only
  client: REST control plane (create/kill) plus the envd Connect-RPC
  data plane for command execution (server-streaming JSON over plain
  HTTP POST — no websockets, same class of hand-rolled protocol as the
  SSE reader and Matrix sync).

House rules honored here:

- stdlib only; fail closed on unknown response shapes and missing auth;
- the host environment is NEVER forwarded into a sandbox (no ``-e``
  flags to docker, an empty ``envs`` map to E2B) — a sandbox that needs
  credentials must receive them by an explicit in-sandbox mechanism the
  user drives, never implicitly from conch's process environment;
- unset config = feature absent: nothing here is imported at session
  start unless ``exec_backend`` is set or ``/sandbox`` is used.
"""

from __future__ import annotations

import atexit
import base64
import json
import shutil
import socket
import struct
import subprocess
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple


class SandboxError(Exception):
    """A backend could not be built or a sandboxed command could not run."""


# ---------------------------------------------------------------------------
# Docker: ephemeral local container, commands via ``docker exec`` argv.
# ---------------------------------------------------------------------------

class DockerExecBackend:
    """One ephemeral container per session.

    The container is created lazily on the first command and removed on
    close/exit. The working directory can be bind-mounted at /workspace
    (``sandbox_docker_mount``: ``rw`` default / ``ro`` / ``none``) so
    builds see the project; ``none`` gives a fully isolated scratch box.
    The container gets a clean environment — nothing from conch's
    process environment is forwarded.
    """

    kind = "docker"

    def __init__(self, config: dict, cwd: Optional[str] = None,
                 image: Optional[str] = None):
        self.image = (
            image
            or str(config.get("sandbox_docker_image") or "").strip()
            or "python:3.12-slim"
        )
        mount = str(config.get("sandbox_docker_mount") or "rw").strip().lower()
        if mount not in ("rw", "ro", "none"):
            raise SandboxError(
                f"sandbox_docker_mount must be rw, ro, or none (got {mount!r})"
            )
        self.mount = mount
        self._cwd = cwd
        self._container: Optional[str] = None
        if not shutil.which("docker"):
            raise SandboxError(
                "docker not found on PATH — install Docker or use a "
                "different exec backend"
            )

    def describe(self) -> str:
        mounted = (
            f"cwd mounted {self.mount} at /workspace"
            if self.mount != "none" else "no mount (isolated scratch)"
        )
        state = self._container[:12] if self._container else "not started"
        return f"docker sandbox — image {self.image}, {mounted} ({state})"

    def _ensure(self) -> str:
        if self._container:
            return self._container
        name = f"conch-sbx-{uuid.uuid4().hex[:12]}"
        argv = ["docker", "run", "-d", "--rm", "--name", name]
        if self.mount != "none" and self._cwd:
            argv += ["-v", f"{self._cwd}:/workspace:{self.mount}",
                     "-w", "/workspace"]
        argv += [self.image, "sleep", "infinity"]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=180
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"docker run failed: {exc}")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise SandboxError(f"docker run failed: {detail}")
        self._container = name
        atexit.register(self.close)
        return name

    def wrap_argv(self, cmd: str) -> List[str]:
        """The command as a ``docker exec`` argv (streamed by the caller
        through the normal PTY path). No ``-e`` flags: clean env."""
        container = self._ensure()
        return ["docker", "exec", "-i", container, "sh", "-lc", cmd]

    def run(self, cmd: str, timeout: int) -> Tuple[str, int]:
        # Not used (wrap_argv covers docker); present for interface parity.
        argv = self.wrap_argv(cmd)
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=timeout if timeout > 0 else 60,
            )
        except subprocess.TimeoutExpired:
            return (f"Command timed out after {timeout}s", 124)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"docker exec failed: {exc}")
        return ((proc.stdout or "") + (proc.stderr or ""), proc.returncode)

    def close(self) -> None:
        container, self._container = self._container, None
        if not container:
            return
        try:
            subprocess.run(
                ["docker", "rm", "-f", container],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass


# ---------------------------------------------------------------------------
# E2B: cloud sandbox over the documented REST + Connect-RPC surface.
# ---------------------------------------------------------------------------

_CONNECT_END_FLAG = 0x02


def _connect_frames(body_bytes_reader, max_payload: int = 8 * 1024 * 1024):
    """Yield ``(flags, payload_json)`` from a Connect streaming body.

    Envelope: 1 flags byte + 4-byte big-endian length + JSON payload.
    Fails closed on oversized or non-JSON payloads.
    """
    while True:
        header = body_bytes_reader(5)
        if not header:
            return
        if len(header) < 5:
            raise SandboxError("truncated Connect frame header")
        flags, length = header[0], struct.unpack(">I", header[1:5])[0]
        if length > max_payload:
            raise SandboxError(f"Connect frame too large ({length} bytes)")
        payload = body_bytes_reader(length) if length else b""
        if len(payload) < length:
            raise SandboxError("truncated Connect frame payload")
        try:
            data = json.loads(payload.decode("utf-8")) if payload else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError(f"malformed Connect frame: {exc}")
        yield flags, data


def _read_exactly(resp, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = resp.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class E2BExecBackend:
    """E2B cloud sandbox, stdlib-only.

    Control plane: ``POST/DELETE https://api.e2b.app/sandboxes`` with the
    API key from the env var named by ``e2b_api_key_env`` (default
    ``E2B_API_KEY``; the key is read by reference and never logged).
    Data plane: ``POST https://sandbox.{domain}/process.Process/Start``
    (Connect server-streaming JSON) with the sandbox-scoped
    ``X-Access-Token`` returned at create time.

    v1 scope, stated plainly: the sandbox does NOT see local files —
    it is a clean remote environment (clone your repo inside it). The
    fleet-worker route is the follow-up for local-artifact workloads.
    """

    kind = "e2b"
    ENVD_PORT = 49983

    def __init__(self, config: dict, _urlopen=None):
        import os

        key_env = str(config.get("e2b_api_key_env") or "E2B_API_KEY").strip()
        self._api_key = os.environ.get(key_env, "")
        if not self._api_key:
            raise SandboxError(
                f"no E2B API key: set {key_env} in the environment "
                "(key is used by reference, never stored in config)"
            )
        self.template = str(config.get("e2b_template") or "base").strip()
        self.api_base = (
            str(config.get("e2b_api_url") or "https://api.e2b.app").rstrip("/")
        )
        try:
            self.ttl = max(60, int(config.get("e2b_timeout_seconds") or 600))
        except (TypeError, ValueError):
            self.ttl = 600
        self._urlopen = _urlopen or urllib.request.urlopen
        self._sandbox_id: Optional[str] = None
        self._domain = ""
        self._envd_token = ""

    def describe(self) -> str:
        state = self._sandbox_id or "not started"
        return f"e2b sandbox — template {self.template} ({state})"

    # -- control plane ------------------------------------------------------

    def _api(self, method: str, path: str, payload: Optional[dict] = None,
             timeout: int = 60) -> dict:
        req = urllib.request.Request(
            f"{self.api_base}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "X-API-KEY": self._api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise SandboxError(
                f"E2B API {method} {path} failed: HTTP {exc.code} {detail}"
            )
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise SandboxError(f"E2B API unreachable: {exc}")
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError(f"E2B API returned malformed JSON: {exc}")
        if not isinstance(data, dict):
            raise SandboxError("E2B API returned an unexpected shape")
        return data

    def _ensure(self) -> str:
        if self._sandbox_id:
            return self._sandbox_id
        data = self._api("POST", "/sandboxes", {
            "templateID": self.template,
            "timeout": self.ttl,
        })
        sandbox_id = str(data.get("sandboxID") or data.get("sandboxId") or "")
        domain = str(data.get("domain") or "e2b.app")
        token = str(
            data.get("envdAccessToken") or data.get("envd_access_token") or ""
        )
        if not sandbox_id or not token:
            # Fail closed rather than guessing at an undocumented shape.
            raise SandboxError(
                "E2B create response missing sandboxID/envdAccessToken — "
                "API shape changed; conch's E2B client needs updating"
            )
        self._sandbox_id = sandbox_id
        self._domain = domain
        self._envd_token = token
        atexit.register(self.close)
        return sandbox_id

    # -- data plane ---------------------------------------------------------

    def wrap_argv(self, cmd: str) -> Optional[List[str]]:
        return None  # no local argv form; run() handles execution

    def run(self, cmd: str, timeout: int) -> Tuple[str, int]:
        self._ensure()
        effective = timeout if timeout > 0 else 60
        start_msg = {
            "process": {
                "cmd": "/bin/sh",
                "args": ["-lc", cmd],
                # Deliberately empty: the host environment is never
                # forwarded into a sandbox.
                "envs": {},
                "cwd": "/home/user",
            }
        }
        payload = json.dumps(start_msg).encode("utf-8")
        body = b"\x00" + struct.pack(">I", len(payload)) + payload
        req = urllib.request.Request(
            f"https://sandbox.{self._domain}/process.Process/Start",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/connect+json",
                "Connect-Protocol-Version": "1",
                "Connect-Timeout-Ms": str(int(effective * 1000)),
                "E2b-Sandbox-Id": self._sandbox_id or "",
                "E2b-Sandbox-Port": str(self.ENVD_PORT),
                "X-Access-Token": self._envd_token,
            },
        )
        out_parts: List[str] = []
        exit_code: Optional[int] = None
        try:
            with self._urlopen(req, timeout=effective + 30) as resp:
                reader = lambda n: _read_exactly(resp, n)  # noqa: E731
                for flags, data in _connect_frames(reader):
                    if flags & _CONNECT_END_FLAG:
                        err = data.get("error") if isinstance(data, dict) else None
                        if err:
                            raise SandboxError(
                                f"envd stream error: "
                                f"{json.dumps(err)[:300]}"
                            )
                        break
                    event = data.get("event") if isinstance(data, dict) else None
                    if not isinstance(event, dict):
                        continue
                    if "data" in event and isinstance(event["data"], dict):
                        for stream in ("stdout", "stderr"):
                            chunk = event["data"].get(stream)
                            if chunk:
                                try:
                                    out_parts.append(
                                        base64.b64decode(chunk).decode(
                                            "utf-8", "replace"
                                        )
                                    )
                                except (ValueError, TypeError):
                                    out_parts.append(str(chunk))
                    end = event.get("end")
                    if isinstance(end, dict):
                        code = end.get("exitCode", end.get("exit_code", 0))
                        try:
                            exit_code = int(code or 0)
                        except (TypeError, ValueError):
                            exit_code = 0
                    # start/keepalive/unknown events: ignored by design
        except urllib.error.HTTPError as exc:
            raise SandboxError(f"envd Start failed: HTTP {exc.code}")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise SandboxError(f"sandbox unreachable mid-command: {exc}")
        if exit_code is None:
            raise SandboxError(
                "sandbox stream ended without a process end event"
            )
        return ("".join(out_parts), exit_code)

    def close(self) -> None:
        sandbox_id, self._sandbox_id = self._sandbox_id, None
        if not sandbox_id:
            return
        try:
            self._api("DELETE", f"/sandboxes/{sandbox_id}", timeout=30)
        except SandboxError:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

BACKEND_KINDS = ("docker", "e2b")


def build_exec_backend(kind: str, config: dict, cwd: Optional[str] = None,
                       **kwargs):
    """Build a backend by name; raises SandboxError with an actionable
    message when the backend cannot be constructed (missing docker,
    missing API key). ``local``/empty means no backend (returns None)."""
    kind = (kind or "").strip().lower()
    if kind in ("", "local", "off"):
        return None
    if kind == "docker":
        return DockerExecBackend(config, cwd=cwd, **kwargs)
    if kind == "e2b":
        return E2BExecBackend(config, **kwargs)
    raise SandboxError(
        f"unknown exec backend {kind!r} (known: local, docker, e2b)"
    )
