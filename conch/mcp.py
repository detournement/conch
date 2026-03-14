"""Minimal MCP transport with safer error handling."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch" / "mcp.json"


@dataclass
class ToolExecutionResult:
    ok: bool
    content: str
    raw: Any = None
    error: str = ""


class HttpMcpClient:
    name = "http"

    def __init__(self, client_name: str, url: str):
        self.name = client_name
        self.url = url.rstrip("/")

    def list_tools(self) -> List[dict]:
        return []

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        payload = {"tool_name": tool_name, "arguments": arguments}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode())
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"MCP HTTP error: {exc}"}]}

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
    tools: List[dict] = []
    tool_map: Dict[str, Any] = {}
    for client in clients.values():
        try:
            client_tools = client.list_tools()
        except Exception:
            client_tools = []
        for tool in client_tools:
            tools.append(tool)
            tool_map[tool["function"]["name"]] = client
    return tools, tool_map


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

