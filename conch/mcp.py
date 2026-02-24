"""MCP (Model Context Protocol) client for Conch. Stdlib only.

Supports stdio and HTTP (Streamable HTTP) transports. Connects to any
MCP-compliant server — Composio, filesystem, custom tools, etc.

Config lives at ~/.config/conch/mcp.json (or $CONCH_MCP_CONFIG).
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MCP_CONFIG_PATHS = [
    Path(os.environ.get("CONCH_MCP_CONFIG", "") or "/dev/null"),
    Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))) / "conch" / "mcp.json",
]

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "conch", "version": "1.0.0"}


def _find_mcp_config() -> Optional[Path]:
    for p in MCP_CONFIG_PATHS:
        try:
            if p.is_file():
                return p
        except (OSError, ValueError):
            continue
    return None


def load_mcp_config() -> dict:
    path = _find_mcp_config()
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


class StdioMCPClient:
    """MCP client that communicates via stdin/stdout with a subprocess."""

    def __init__(self, name: str, command: str, args: List[str] = None,
                 env: Dict[str, str] = None):
        self.name = name
        self._id = 0
        merged_env = {**os.environ, **(env or {})}
        self._proc = subprocess.Popen(
            [command] + (args or []),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        self._initialize()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _notify(self, method: str, params: dict = None):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        self._proc.stdin.flush()

    def _send(self, method: str, params: dict = None) -> dict:
        request_id = self._next_id()
        msg: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            msg["params"] = params
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        self._proc.stdin.flush()

        while True:
            line = self._proc.stdout.readline()
            if not line:
                return {"error": {"message": "MCP server closed connection"}}
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == request_id:
                return resp

    def _initialize(self):
        resp = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self._notify("notifications/initialized")
        return resp

    def list_tools(self) -> List[dict]:
        resp = self._send("tools/list")
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = self._send("tools/call", {"name": name, "arguments": arguments})
        if "error" in resp:
            return {"error": resp["error"]}
        return resp.get("result", {})

    def close(self):
        try:
            self._proc.stdin.close()
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


class HttpMCPClient:
    """MCP client for HTTP-based (Streamable HTTP) servers like Composio."""

    def __init__(self, name: str, url: str, headers: Dict[str, str] = None):
        self.name = name
        self.url = url.rstrip("/")
        self._headers = headers or {}
        self._id = 0
        self._session_id: Optional[str] = None
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._initialize()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict, timeout: int = 60) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "conch/1.0",
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid

                content_type = r.headers.get("Content-Type", "")
                body = r.read().decode()

                if "text/event-stream" in content_type:
                    for line in body.splitlines():
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if not data_str:
                                continue
                            try:
                                return json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                    return {"error": {"message": "No JSON in SSE response"}}
                return json.loads(body)
        except Exception as e:
            return {"error": {"message": str(e)}}

    def _send(self, method: str, params: dict = None) -> dict:
        msg: dict = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params:
            msg["params"] = params
        return self._post(msg)

    def _initialize(self):
        resp = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        # Send initialized notification
        notif: dict = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            self._post(notif, timeout=10)
        except Exception:
            pass
        return resp

    def list_tools(self) -> List[dict]:
        resp = self._send("tools/list")
        return resp.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        resp = self._send("tools/call", {"name": name, "arguments": arguments})
        if "error" in resp:
            return {"error": resp["error"]}
        return resp.get("result", {})

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_clients() -> list:
    """Load mcp.json and create a client for every configured server."""
    config = load_mcp_config()
    servers = config.get("mcpServers", {})
    clients: list = []

    for name, sc in servers.items():
        try:
            transport = sc.get("type", "stdio")
            if transport == "stdio":
                client = StdioMCPClient(
                    name=name,
                    command=sc["command"],
                    args=sc.get("args", []),
                    env=sc.get("env"),
                )
            elif transport in ("http", "sse", "streamable-http"):
                client = HttpMCPClient(
                    name=name,
                    url=sc["url"],
                    headers=sc.get("headers"),
                )
            else:
                print(f"conch: unknown MCP transport '{transport}' for '{name}'",
                      file=sys.stderr)
                continue
            clients.append(client)
        except Exception as e:
            print(f"conch: MCP server '{name}' failed: {e}", file=sys.stderr)

    return clients


def collect_tools(clients: list) -> Tuple[List[dict], Dict[str, Any]]:
    """Gather tools from every client.

    Returns
    -------
    openai_tools : list
        Tool definitions in OpenAI function-calling format.
    tool_map : dict
        {tool_name: client} so we can route calls.
    """
    openai_tools: List[dict] = []
    tool_map: Dict[str, Any] = {}

    for client in clients:
        try:
            for t in client.list_tools():
                name = t.get("name", "")
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": t.get("description", ""),
                        "parameters": t.get("inputSchema",
                                            {"type": "object", "properties": {}}),
                    },
                })
                tool_map[name] = client
        except Exception as e:
            print(f"conch: listing tools from '{client.name}': {e}",
                  file=sys.stderr)

    return openai_tools, tool_map


def execute_tool(tool_map: dict, name: str, arguments: dict) -> str:
    """Run an MCP tool and return result text."""
    client = tool_map.get(name)
    if not client:
        return f"Error: unknown tool '{name}'"
    try:
        result = client.call_tool(name, arguments)
        content = result.get("content", [])
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, dict):
                    parts.append(json.dumps(block))
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else json.dumps(result)
        return str(content) if content else json.dumps(result)
    except Exception as e:
        return f"Error executing '{name}': {e}"


def close_all(clients: list):
    """Shut down every client cleanly."""
    for c in clients:
        try:
            c.close()
        except Exception:
            pass
