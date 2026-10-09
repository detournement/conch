"""Minimal MCP transport with safer error handling."""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch" / "mcp.json"

_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"
_TOOL_CACHE_PATH = _STATE_DIR / "tool_cache.json"
_CACHE_TTL = 1800  # 30 minutes

# Discovery bounds. ``initialize`` is the first packet a server ever sees,
# so it is where an unreachable host shows up: a LAN address that is not on
# the current network drops the SYN and costs the whole connect timeout.
# Before these bounds the handshake and tools/list each waited the full
# request timeout (30s + 30s) and a hung stdio server blocked forever.
DEFAULT_CONNECT_TIMEOUT = 5.0  # HTTP initialize: the reachability probe
DEFAULT_REQUEST_TIMEOUT = 30.0  # HTTP tools/list, tools/call on a live server
DEFAULT_STDIO_READ_TIMEOUT = 60.0  # one reply from a stdio server


@dataclass
class ToolExecutionResult:
    ok: bool
    content: str
    raw: Any = None
    error: str = ""


@dataclass
class ServerStatus:
    """Discovery outcome for one MCP server (``/tools``, startup notices).

    ``state`` is ``loading`` until the server's ``tools/list`` finished,
    then ``ready``, ``unreachable`` (it never answered: connect refused or
    timed out, process gone, no reply in time) or ``error`` (it answered
    with a failure; ``detail`` carries the reason).
    """

    name: str
    state: str = "loading"
    detail: str = ""
    tool_count: int = 0
    elapsed: float = 0.0

    def describe(self) -> str:
        if self.state == "ready":
            plural = "" if self.tool_count == 1 else "s"
            return f"ready ({self.tool_count} tool{plural}, {self.elapsed:.1f}s)"
        if self.state == "loading":
            return "loading"
        return f"{self.state}: {self.detail}" if self.detail else self.state


def _transport_error(message: str) -> dict:
    """JSON-RPC-shaped error for "the server did not answer at all" —
    connect refused or timed out, pipe closed, no reply within the bound.
    Distinct from a server that answered with an error: a transport
    failure on the handshake means the real request must not be sent."""
    return {"error": {"message": message, "transport": True}}


def is_transport_error(message: Any) -> bool:
    error = message.get("error") if isinstance(message, dict) else None
    return isinstance(error, dict) and bool(error.get("transport"))


def _describe_network_error(exc: BaseException, timeout: float) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
        return f"timed out after {timeout:g}s"
    return str(reason) or exc.__class__.__name__


MCP_PROTOCOL_VERSION = "2025-06-18"
_SESSION_HEADER = "mcp-session-id"


def _parse_sse(body: str, request_id: Any = None) -> Optional[dict]:
    """The JSON-RPC message for *request_id* from an SSE body.

    A Streamable HTTP response may carry several events (progress or log
    notifications before the actual response); prefer the one whose ``id``
    matches, else the last parseable message.
    """
    last: Optional[dict] = None
    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            message = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if request_id is not None and message.get("id") == request_id:
            return message
        last = message
    return last


def _looks_like_session_error(response: dict) -> bool:
    error = response.get("error")
    if not isinstance(error, dict):
        return False
    message = str(error.get("message", "")).lower()
    return "session" in message and (
        "missing" in message or "invalid" in message or "not found" in message
        or "expired" in message or "terminated" in message
    )


class HttpMcpClient:
    """MCP client that communicates over HTTP using JSON-RPC.

    Speaks the Streamable HTTP transport: the first call performs the
    ``initialize`` handshake, keeps the ``mcp-session-id`` the server
    issues and sends it on every later request. Servers that answer
    plain JSON-RPC without sessions keep working — a server that
    *rejects* ``initialize`` is used as-is. A server that never answers
    it (``connect_timeout``, default 5s) is marked unreachable and gets
    no further request: the old fall-through to ``tools/list`` doubled
    the wait for every dead host.
    """

    name = "http"

    def __init__(self, client_name: str, url: str,
                 headers: Optional[dict] = None,
                 connect_timeout: Optional[float] = None,
                 timeout: Optional[float] = None):
        self.name = client_name
        self.url = url.rstrip("/")
        # Extra request headers (e.g. an Authorization bearer for hosted
        # MCP servers); values are used by reference and never logged.
        self.headers = dict(headers or {})
        self.connect_timeout = float(
            DEFAULT_CONNECT_TIMEOUT if connect_timeout is None else connect_timeout
        )
        self.timeout = float(DEFAULT_REQUEST_TIMEOUT if timeout is None else timeout)
        self._next_request_id = 1
        self._session_id: Optional[str] = None
        self._handshake_done = False
        self.server_info: Dict[str, Any] = {}
        # Discovery status (read by ToolDiscovery / ``/tools``): the last
        # failure message ("" after a success) and whether the server
        # never answered the handshake at all.
        self.last_error = ""
        self.unreachable = False

    def _post(self, payload: dict,
              timeout: Optional[float] = None) -> Tuple[dict, Dict[str, str]]:
        """POST one JSON-RPC message; return (message, response headers).

        Supports both plain JSON and SSE (Streamable HTTP) responses, as
        required by the MCP HTTP transport spec. Transport failures come
        back as a JSON-RPC-shaped ``{"error": {...}}`` so callers have one
        code path; the ones where the server never answered are flagged
        (see :func:`is_transport_error`).
        """
        timeout = self.timeout if timeout is None else timeout
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "conch/1.0",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            request_headers[_SESSION_HEADER] = self._session_id
        request_headers.update(self.headers)
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                response_headers = {
                    str(k).lower(): str(v) for k, v in response.headers.items()
                }
                ct = response_headers.get("content-type", "")
                body = response.read().decode("utf-8", errors="replace")
                if not body.strip():
                    # 202 Accepted: the server took a notification.
                    return {}, response_headers
                if "event-stream" in ct:
                    message = _parse_sse(body, payload.get("id"))
                    if message is None:
                        return {"error": {"message": "No valid SSE data received"}}, response_headers
                    return message, response_headers
                return json.loads(body), response_headers
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                exc.close()
            except Exception:
                pass
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                return parsed, {}
            return {"error": {"message": f"HTTP {exc.code}: {detail or exc.reason}"}}, {}
        except OSError as exc:
            # URLError (refused, unreachable, DNS), socket timeouts, resets:
            # the server never answered.
            return _transport_error(_describe_network_error(exc, timeout)), {}
        except Exception as exc:
            return {"error": {"message": str(exc)}}, {}

    def _handshake(self) -> bool:
        """Streamable HTTP session setup: initialize → session id →
        initialized notification. Never raises.

        Returns False only when the server never answered (transport
        failure within ``connect_timeout``); the caller must then not send
        its real request. A server that answers ``initialize`` with an
        error is a plain JSON-RPC server and is used as-is (True).
        """
        self._handshake_done = True
        self._session_id = None
        self.unreachable = False
        request_id = self._next_request_id
        self._next_request_id += 1
        message, response_headers = self._post({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conch", "version": "1.0"},
            },
        }, timeout=self.connect_timeout)
        if is_transport_error(message):
            self.unreachable = True
            self.last_error = str(message["error"].get("message", "no answer"))
            return False
        if "error" in message:
            return True
        result = message.get("result") or {}
        if isinstance(result, dict):
            info = result.get("serverInfo")
            if isinstance(info, dict):
                self.server_info = dict(info)
        session_id = response_headers.get(_SESSION_HEADER, "").strip()
        if session_id:
            self._session_id = session_id
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC request to the MCP HTTP server.

        Performs the session handshake on first use (and again on every
        call while the server stays unreachable, so it can come back),
        and once more when the server reports the session gone (restart,
        expiry), replaying the request a single time. A handshake the
        server never answered ends the call right there.
        """
        if not self._handshake_done or self.unreachable:
            if not self._handshake():
                return _transport_error(self.last_error)
        request_id = self._next_request_id
        self._next_request_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        message, _ = self._post(payload)
        if _looks_like_session_error(message):
            if not self._handshake():
                return _transport_error(self.last_error)
            message, _ = self._post(payload)
        return message

    def list_tools(self) -> List[dict]:
        """Query the server for available tools via JSON-RPC.

        Returns [] on failure and records why in ``last_error``."""
        response = self._rpc("tools/list")
        if "error" in response:
            self.last_error = str(response["error"].get("message", "unknown error"))
            return []
        self.last_error = ""
        raw_tools = response.get("result", {}).get("tools", [])
        tools = []
        for tool in raw_tools:
            name = tool.get("name", "")
            if not name:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
        return tools

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        response = self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if "error" in response:
            msg = response["error"].get("message", "unknown")
            return {"content": [{"type": "text", "text": "MCP HTTP error: " + msg}]}
        return response.get("result", {"content": [{"type": "text", "text": "(no result)"}]})

    def close(self):
        return None


class StdioMcpClient:
    """MCP client over a child process's stdin/stdout.

    Every reply is waited for with a deadline (``read_timeout``, default
    60s): a server that never answers surfaces as a transport error
    instead of blocking the caller forever. A process that cannot be
    started is recorded as unreachable rather than raised, so one broken
    entry in mcp.json cannot take the other servers down with it.
    """

    def __init__(self, client_name: str, command: str,
                 args: Optional[List[str]] = None,
                 env: Optional[dict] = None,
                 read_timeout: Optional[float] = None):
        self.name = client_name
        self.read_timeout = float(
            DEFAULT_STDIO_READ_TIMEOUT if read_timeout is None else read_timeout
        )
        self._next_request_id = 1
        self._ready = False
        self.last_error = ""
        self.unreachable = False
        self._proc: Optional[subprocess.Popen] = None
        # Replies arrive through a reader thread that owns stdout: _send
        # waits on this queue with its deadline, and lines the child
        # wrote together (a banner plus the reply) are never stranded in
        # a buffer the way a select() on the raw pipe would leave them.
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        # Inherit the parent environment so PATH/HOME still resolve, then
        # layer the server's own env (credentials) on top. Without this,
        # stdio servers configured purely via env (Jira, GitHub, ...) start
        # unauthenticated and advertise zero tools.
        proc_env = dict(os.environ)
        proc_env.update(_expand_env(env))
        # stderr -> DEVNULL: these servers emit banners/warnings, and an
        # undrained PIPE fills its buffer and deadlocks the child.
        try:
            self._proc = subprocess.Popen(
                [command] + (args or []),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=proc_env,
            )
        except (OSError, ValueError) as exc:
            self.unreachable = True
            self.last_error = f"cannot start {command}: {exc}"
            return
        self._reader = threading.Thread(
            target=self._pump_stdout, daemon=True,
            name=f"mcp-stdio-{client_name}",
        )
        self._reader.start()

    def _pump_stdout(self) -> None:
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            self._lines.put(None)
            return
        try:
            for line in iter(stdout.readline, ""):
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)  # EOF sentinel: the server is gone

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """Fire-and-forget JSON-RPC notification (no id, no reply)."""
        if self._proc is None or not self._proc.stdin:
            return
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def _handshake(self) -> bool:
        """Perform the MCP initialize exchange once, before any real call.

        Spec-compliant servers (anything on FastMCP) refuse tools/list until
        initialize has completed, which previously surfaced as "0 tools".
        Returns False when the server never answered ``initialize`` (dead
        process, no reply within ``read_timeout``): the server is marked
        unreachable for the life of this client — a fresh client (``/reload``)
        starts a fresh process — and the real request is not sent.
        """
        if self._ready:
            return not self.unreachable
        self._ready = True
        response = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "conch", "version": "0.6.0"},
        })
        if is_transport_error(response):
            self.unreachable = True
            self.last_error = str(response["error"].get("message", "no answer"))
            return False
        self._notify("notifications/initialized")
        return True

    def _send(self, method: str, params: Optional[dict] = None) -> dict:
        if self._proc is None:
            return _transport_error(self.last_error or "stdio server not started")
        if not self._proc.stdin or not self._proc.stdout:
            return {"error": {"message": "stdio client not initialized"}}
        request_id = self._next_request_id
        self._next_request_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            # Broken pipe: the process is gone.
            return _transport_error(f"failed to send MCP request: {exc}")
        except Exception as exc:
            return {"error": {"message": f"failed to send MCP request: {exc}"}}

        deadline = time.monotonic() + self.read_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _transport_error(
                    f"no reply to {method} within {self.read_timeout:g}s"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return _transport_error(
                    f"no reply to {method} within {self.read_timeout:g}s"
                )
            if line is None:
                self._lines.put(None)  # keep EOF visible to later callers
                code = self._proc.poll()
                detail = f" (exit code {code})" if code is not None else ""
                return _transport_error(f"MCP server closed connection{detail}")
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("id") == request_id:
                return data

    def list_tools(self) -> List[dict]:
        """Returns [] on failure and records why in ``last_error``."""
        if not self._handshake():
            return []
        response = self._send("tools/list")
        if "error" in response:
            self.last_error = str(response["error"].get("message", "unknown error"))
            return []
        self.last_error = ""
        raw_tools = response.get("result", {}).get("tools", [])
        tools = []
        for tool in raw_tools:
            name = tool.get("name", "")
            if not name:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
        return tools

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        if not self._handshake():
            return {"content": [{"type": "text", "text": (
                f"MCP server {self.name} unreachable: {self.last_error}"
            )}]}
        response = self._send("tools/call", {"name": tool_name, "arguments": arguments})
        return response.get("result", {"content": [{"type": "text", "text": response.get("error", {}).get("message", "Unknown MCP error")}]} )

    def close(self):
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=1)
        finally:
            for stream in (self._proc.stdin, self._proc.stdout):
                try:
                    stream.close()
                except Exception:
                    pass


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"mcpServers": {}}


def _expand_env(values: Optional[dict]) -> Dict[str, str]:
    """Expand ${VAR} / $VAR in MCP config values from the environment.

    Used for both HTTP ``headers`` and stdio ``env`` blocks, so mcp.json can
    reference secrets by name instead of storing them in plaintext. Entries
    whose variables are unset are dropped, so a missing credential surfaces
    as an auth error rather than a literal "${VAR}".
    """
    out: Dict[str, str] = {}
    for key, value in (values or {}).items():
        if not isinstance(value, str):
            continue
        expanded = os.path.expandvars(value)
        if "$" in expanded and expanded != value:
            continue
        if re.fullmatch(r"\$\{?\w+\}?", expanded):
            continue
        out[str(key)] = expanded
    return out


CAPITOL_DOCS_SERVER_NAME = "capitol-docs"


def capitol_docs_url(config: Optional[dict]) -> str:
    """The Capitol docs MCP endpoint from the conch config (``capitol_docs_url``),
    or "" when unset. The docs server answers "what is X / how do I X" for
    the release an environment runs; pointing conch at it is one config
    key next to ``capitol_base_url`` rather than a hand-written mcp.json
    block. Unset = the server is absent: zero traffic."""
    if not config:
        return ""
    return str(config.get("capitol_docs_url") or "").strip()


def _timeout_option(name: str, cfg: dict, key: str) -> Optional[float]:
    """A positive number of seconds from a server's mcp.json block, or
    None for the default. A value that is not one is a configuration
    error and says so (fail loud) instead of silently becoming a
    default the user did not ask for."""
    value = cfg.get(key)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        raise ValueError(
            f"mcp.json: server {name!r} has {key}={value!r}; "
            "it must be a positive number of seconds"
        )
    return seconds


def _server_entries() -> List[Tuple[str, dict]]:
    """(name, block) for every mcp.json entry that yields a client, in
    config order."""
    entries: List[Tuple[str, dict]] = []
    for name, cfg in _load_config().get("mcpServers", {}).items():
        if not isinstance(cfg, dict):
            continue
        if (cfg.get("type") == "http" and cfg.get("url")) or cfg.get("command"):
            entries.append((str(name), cfg))
    return entries


def configured_server_names(config: Optional[dict] = None) -> List[str]:
    """Names of the MCP servers a session will try, in config order,
    without constructing clients (no process is spawned): the startup
    loader reports these as "loading" before discovery has reached them."""
    names = [name for name, _ in _server_entries()]
    if capitol_docs_url(config) and CAPITOL_DOCS_SERVER_NAME not in names:
        names.append(CAPITOL_DOCS_SERVER_NAME)
    return names


def create_clients() -> Dict[str, Any]:
    """MCP clients from ``mcp.json`` (config-keyed servers are added by
    :func:`mount_config_servers`).

    Optional per-server keys: ``connect_timeout`` (HTTP handshake bound,
    default 5s), ``timeout`` (HTTP request bound, default 30s; for stdio
    servers the per-reply read bound, default 60s).
    """
    clients: Dict[str, Any] = {}
    for name, cfg in _server_entries():
        if cfg.get("type") == "http" and cfg.get("url"):
            clients[name] = HttpMcpClient(
                name, cfg["url"], _expand_env(cfg.get("headers")),
                connect_timeout=_timeout_option(name, cfg, "connect_timeout"),
                timeout=_timeout_option(name, cfg, "timeout"),
            )
        elif cfg.get("command"):
            clients[name] = StdioMcpClient(
                name, cfg["command"], cfg.get("args", []), cfg.get("env"),
                read_timeout=_timeout_option(name, cfg, "timeout"),
            )
    return clients


def mount_config_servers(clients: Dict[str, Any], config: Optional[dict]) -> Dict[str, Any]:
    """Add the MCP servers the conch config names (today: the Capitol
    docs server via ``capitol_docs_url``) to *clients*, in place.

    An explicit ``mcp.json`` entry of the same name wins — it may carry
    auth headers; the config key is the zero-ceremony path.
    """
    docs_url = capitol_docs_url(config)
    if docs_url and CAPITOL_DOCS_SERVER_NAME not in clients:
        clients[CAPITOL_DOCS_SERVER_NAME] = HttpMcpClient(
            CAPITOL_DOCS_SERVER_NAME, docs_url)
    return clients


def _client_status(name: str, client: Any, tools: List[dict],
                   error: str, elapsed: float = 0.0) -> ServerStatus:
    if not error:
        state = "ready"
    elif getattr(client, "unreachable", False):
        state = "unreachable"
    else:
        state = "error"
    return ServerStatus(name, state, error, len(tools), elapsed)


class ToolDiscovery:
    """Parallel ``tools/list`` across MCP clients with per-server status.

    One thread per server, each bounded by its client's own timeouts, so
    one dead server cannot delay the others. :meth:`statuses` is safe to
    read from another thread while :meth:`run` is in progress — the
    startup loader uses it to name the servers still loading when its
    wait expires, and ``/tools`` to show ready / loading / unreachable.
    """

    def __init__(self, clients: Dict[str, Any]):
        self.clients: Dict[str, Any] = dict(clients)
        self._lock = threading.Lock()
        self._statuses = {name: ServerStatus(name) for name in self.clients}
        self._results: Dict[str, List[dict]] = {}

    def statuses(self) -> Dict[str, ServerStatus]:
        """Snapshot (copies) in config order."""
        with self._lock:
            return {name: replace(status) for name, status in self._statuses.items()}

    def pending(self) -> List[str]:
        with self._lock:
            return [n for n, s in self._statuses.items() if s.state == "loading"]

    def _discover(self, name: str, client: Any) -> None:
        started = time.monotonic()
        try:
            tools = client.list_tools()
            error = str(getattr(client, "last_error", "") or "")
        except Exception as exc:  # a client that raises is an error, not a hang
            tools, error = [], f"{exc.__class__.__name__}: {exc}"
        status = _client_status(name, client, tools, error, time.monotonic() - started)
        with self._lock:
            self._results[name] = list(tools)
            self._statuses[name] = status

    def run(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Discover every server (blocking until each has finished or hit
        its bound) and return ``(tools, tool_map)``."""
        if self.clients:
            with ThreadPoolExecutor(
                max_workers=len(self.clients), thread_name_prefix="mcp-discover"
            ) as pool:
                futures = [
                    pool.submit(self._discover, name, client)
                    for name, client in self.clients.items()
                ]
                for future in futures:
                    future.result()
        return self.merged()

    def merged(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Tools found so far, in config order even though discovery is
        parallel. Duplicate names are skipped after the first server so an
        offered schema can never route to a different, nondeterministically
        selected client."""
        tools: List[dict] = []
        tool_map: Dict[str, Any] = {}
        with self._lock:
            results = {name: list(found) for name, found in self._results.items()}
        for name, client in self.clients.items():
            for tool in results.get(name, []):
                tool_name = tool.get("function", {}).get("name", "")
                if not tool_name or tool_name in tool_map:
                    continue
                tools.append(tool)
                tool_map[tool_name] = client
        return tools, tool_map


def collect_tools(clients: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    """Collect tools from all MCP clients in parallel (see ToolDiscovery)."""
    return ToolDiscovery(clients).run()


def server_statuses(clients: Dict[str, Any],
                    tool_map: Dict[str, Any]) -> Dict[str, ServerStatus]:
    """Final per-server status after a :func:`collect_tools` pass, derived
    from what each client recorded (``last_error`` / ``unreachable``) and
    how many tools routed to it."""
    counts: Dict[str, int] = {}
    for client in tool_map.values():
        for name, candidate in clients.items():
            if candidate is client:
                counts[name] = counts.get(name, 0) + 1
                break
    statuses: Dict[str, ServerStatus] = {}
    for name, client in clients.items():
        error = str(getattr(client, "last_error", "") or "")
        status = _client_status(name, client, [], error)
        status.tool_count = counts.get(name, 0)
        statuses[name] = status
    return statuses


def load_cached_tools() -> Optional[List[dict]]:
    """Load tool definitions from disk cache if still fresh."""
    try:
        data = json.loads(_TOOL_CACHE_PATH.read_text())
        if time.time() - data.get("ts", 0) < _CACHE_TTL:
            return data.get("tools", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def save_tool_cache(tools: List[dict]):
    """Save tool definitions to disk cache."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        defs = [{"type": t.get("type", "function"), "function": t["function"]} for t in tools]
        _TOOL_CACHE_PATH.write_text(json.dumps({"ts": time.time(), "tools": defs}))
    except (OSError, TypeError):
        pass


def execute_tool_result(tool_map: Dict[str, Any], name: str, arguments: dict) -> ToolExecutionResult:
    client = tool_map.get(name)
    if not client:
        return ToolExecutionResult(ok=False, content=f"Unknown tool: {name}", error="tool_not_found")
    raw = client.call_tool(name, arguments)
    content = raw.get("content", [])
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                text_parts.append(str(block.get("text", block.get("content", ""))))
            else:
                text_parts.append(str(block))
        text = "\n".join(part for part in text_parts if part).strip()
    else:
        text = str(content)
    return ToolExecutionResult(ok=True, content=text or "(no output)", raw=raw)


def execute_tool(tool_map: Dict[str, Any], name: str, arguments: dict) -> str:
    return execute_tool_result(tool_map, name, arguments).content


def close_all(clients: Dict[str, Any]):
    for client in clients.values():
        try:
            client.close()
        except Exception:
            pass

