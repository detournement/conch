"""Minimal MCP transport with safer error handling."""

from __future__ import annotations

import json
import os
import subprocess
import time
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


class HttpMcpClient:
    """MCP client that communicates over HTTP using JSON-RPC."""

    name = "http"

    def __init__(self, client_name: str, url: str):
        self.name = client_name
        self.url = url.rstrip("/")
        self._next_request_id = 1

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a JSON-RPC request to the MCP HTTP server.

        Supports both plain JSON and SSE (Streamable HTTP) responses,
        as required by the MCP HTTP transport spec.
        """
        request_id = self._next_request_id
        self._next_request_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "conch/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                ct = response.headers.get("Content-Type", "")
                body = response.read().decode("utf-8", errors="replace")
                if "event-stream" in ct:
                    for line in body.split("\n"):
                        if line.startswith("data: "):
                            try:
                                return json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                    return {"error": {"message": "No valid SSE data received"}}
                return json.loads(body)
        except Exception as exc:
            return {"error": {"message": str(exc)}}

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
    def __init__(self, client_name: str, command: str, args: Optional[List[str]] = None):
        self.name = client_name
        self._proc = subprocess.Popen(
            [command] + (args or []),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._next_request_id = 1

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
        response = self._send("tools/list")
        return response.get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
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


def create_clients() -> Dict[str, Any]:
    clients: Dict[str, Any] = {}
    for name, cfg in _load_config().get("mcpServers", {}).items():
        if cfg.get("type") == "http" and cfg.get("url"):
            clients[name] = HttpMcpClient(name, cfg["url"])
        elif cfg.get("command"):
            clients[name] = StdioMcpClient(name, cfg["command"], cfg.get("args", []))
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

    with ThreadPoolExecutor(max_workers=max(len(clients), 1)) as pool:
        futures = [pool.submit(_load_one, n, c) for n, c in clients.items()]
        for future in as_completed(futures):
            name, client, client_tools = future.result()
            for tool in client_tools:
                tools.append(tool)
                tool_map[tool["function"]["name"]] = client
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

