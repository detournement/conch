"""Minimal MCP transport with safer error handling."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch" / "mcp.json"

_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"
_TOOL_CACHE_PATH = _STATE_DIR / "tool_cache.json"
_CACHE_TTL = 1800  # 30 minutes


@dataclass
class ToolExecutionResult:
    ok: bool
    content: str
    raw: Any = None
    error: str = ""


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
    plain JSON-RPC without sessions keep working — the handshake is
    best-effort and a server that rejects ``initialize`` is used as-is.
    """

    name = "http"

    def __init__(self, client_name: str, url: str,
                 headers: Optional[dict] = None):
        self.name = client_name
        self.url = url.rstrip("/")
        # Extra request headers (e.g. an Authorization bearer for hosted
        # MCP servers); values are used by reference and never logged.
        self.headers = dict(headers or {})
        self._next_request_id = 1
        self._session_id: Optional[str] = None
        self._handshake_done = False
        self.server_info: Dict[str, Any] = {}

    def _post(self, payload: dict) -> Tuple[dict, Dict[str, str]]:
        """POST one JSON-RPC message; return (message, response headers).

        Supports both plain JSON and SSE (Streamable HTTP) responses, as
        required by the MCP HTTP transport spec. Transport failures come
        back as a JSON-RPC-shaped ``{"error": {...}}`` so callers have one
        code path.
        """
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
            with urllib.request.urlopen(req, timeout=30) as response:
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
        except Exception as exc:
            return {"error": {"message": str(exc)}}, {}

    def _handshake(self) -> None:
        """Streamable HTTP session setup: initialize → session id →
        initialized notification. Best-effort; never raises."""
        self._handshake_done = True
        self._session_id = None
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
        })
        if "error" in message:
            return
        result = message.get("result") or {}
        if isinstance(result, dict):
            info = result.get("serverInfo")
            if isinstance(info, dict):
                self.server_info = dict(info)
        session_id = response_headers.get(_SESSION_HEADER, "").strip()
        if session_id:
            self._session_id = session_id
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC request to the MCP HTTP server.

        Performs the session handshake on first use and once more when
        the server reports the session gone (restart, expiry), replaying
        the request a single time.
        """
        if not self._handshake_done:
            self._handshake()
        request_id = self._next_request_id
        self._next_request_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        message, _ = self._post(payload)
        if _looks_like_session_error(message):
            self._handshake()
            message, _ = self._post(payload)
        return message

    def list_tools(self) -> List[dict]:
        """Query the server for available tools via JSON-RPC."""
        response = self._rpc("tools/list")
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
    def __init__(self, client_name: str, command: str,
                 args: Optional[List[str]] = None,
                 env: Optional[dict] = None):
        self.name = client_name
        # Inherit the parent environment so PATH/HOME still resolve, then
        # layer the server's own env (credentials) on top. Without this,
        # stdio servers configured purely via env (Jira, GitHub, ...) start
        # unauthenticated and advertise zero tools.
        proc_env = dict(os.environ)
        proc_env.update(_expand_env(env))
        # stderr -> DEVNULL: these servers emit banners/warnings, and an
        # undrained PIPE fills its buffer and deadlocks the child.
        self._proc = subprocess.Popen(
            [command] + (args or []),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=proc_env,
        )
        self._next_request_id = 1
        self._ready = False

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        """Fire-and-forget JSON-RPC notification (no id, no reply)."""
        if not self._proc.stdin:
            return
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    def _handshake(self) -> None:
        """Perform the MCP initialize exchange once, before any real call.

        Spec-compliant servers (anything on FastMCP) refuse tools/list until
        initialize has completed, which previously surfaced as "0 tools".
        """
        if self._ready:
            return
        self._ready = True
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "conch", "version": "0.6.0"},
        })
        self._notify("notifications/initialized")

    def _send(self, method: str, params: Optional[dict] = None) -> dict:
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
        except Exception as exc:
            return {"error": {"message": f"failed to send MCP request: {exc}"}}

        while True:
            line = self._proc.stdout.readline()
            if not line:
                return {"error": {"message": "MCP server closed connection"}}
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("id") == request_id:
                return data

    def list_tools(self) -> List[dict]:
        self._handshake()
        response = self._send("tools/list")
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
        self._handshake()
        response = self._send("tools/call", {"name": tool_name, "arguments": arguments})
        return response.get("result", {"content": [{"type": "text", "text": response.get("error", {}).get("message", "Unknown MCP error")}]} )

    def close(self):
        self._proc.terminate()
        self._proc.wait(timeout=1)


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


def create_clients() -> Dict[str, Any]:
    """MCP clients from ``mcp.json`` (config-keyed servers are added by
    :func:`mount_config_servers`)."""
    clients: Dict[str, Any] = {}
    for name, cfg in _load_config().get("mcpServers", {}).items():
        if cfg.get("type") == "http" and cfg.get("url"):
            clients[name] = HttpMcpClient(
                name, cfg["url"], _expand_env(cfg.get("headers")))
        elif cfg.get("command"):
            clients[name] = StdioMcpClient(
                name, cfg["command"], cfg.get("args", []), cfg.get("env"))
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


def collect_tools(clients: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    """Collect tools from all MCP clients in parallel."""
    tools: List[dict] = []
    tool_map: Dict[str, Any] = {}
    if not clients:
        return tools, tool_map

    def _load_one(name, client):
        try:
            return name, client, client.list_tools()
        except Exception:
            return name, client, []

    loaded: Dict[str, tuple] = {}
    with ThreadPoolExecutor(max_workers=max(len(clients), 1)) as pool:
        futures = [pool.submit(_load_one, n, c) for n, c in clients.items()]
        for future in as_completed(futures):
            name, client, client_tools = future.result()
            loaded[name] = (client, client_tools)
    # Preserve config order even though discovery is parallel. Duplicate names
    # are skipped after the first server so an offered schema can never route
    # to a different, nondeterministically selected client.
    for name in clients:
        client, client_tools = loaded.get(name, (clients[name], []))
        for tool in client_tools:
            tool_name = tool.get("function", {}).get("name", "")
            if not tool_name or tool_name in tool_map:
                continue
            tools.append(tool)
            tool_map[tool_name] = client
    return tools, tool_map


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

