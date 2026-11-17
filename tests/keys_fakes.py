"""Fake services for the ``conch keys`` tests: one loopback HTTP server
that plays Jira (``/rest/api/3/myself``, Basic auth), Composio
(``/api/v3/toolkits``, ``x-api-key``), E2B (``/sandboxes``,
``X-API-Key``), Slack (``/auth.test``, bearer), the status page
(``/api/ingest``, bearer) and a Streamable-HTTP MCP server (``/mcp``,
bearer; ``initialize`` + ``tools/list``). Provider endpoints come from
:mod:`tests.modelcheck_stubs` (``StubEndpoint``), which already speaks
OpenAI/Anthropic shapes with ``require_token``.

Accepted credentials are configured per service; everything else is a
401 with a realistic body. Nothing here logs a header value.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Set, Tuple


class FakeServices:
    def __init__(self, *, jira_accounts: Optional[Dict[str, str]] = None,
                 composio_keys: Optional[Set[str]] = None,
                 e2b_keys: Optional[Set[str]] = None,
                 slack_tokens: Optional[Set[str]] = None,
                 status_tokens: Optional[Set[str]] = None,
                 mcp_tokens: Optional[Set[str]] = None,
                 mcp_tools: Optional[List[str]] = None):
        self.jira_accounts = dict(jira_accounts or {})
        self.composio_keys = set(composio_keys or ())
        self.e2b_keys = set(e2b_keys or ())
        self.slack_tokens = set(slack_tokens or ())
        self.status_tokens = set(status_tokens or ())
        self.mcp_tokens = set(mcp_tokens or ())
        self.mcp_tools = list(mcp_tools or ["jira_search", "jira_get_issue"])
        self.requests: List[Tuple[str, str]] = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _send(self, status: int, payload, content_type: str = "application/json") -> None:
                body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw.decode() or "{}")
                except ValueError:
                    return {}

            def _bearer(self) -> str:
                value = self.headers.get("Authorization", "")
                return value[len("Bearer "):] if value.startswith("Bearer ") else ""

            def _handle(self, method: str) -> None:
                path = self.path.split("?", 1)[0]
                with fake._lock:
                    fake.requests.append((method, path))
                body = self._body() if method == "POST" else {}
                if path == "/rest/api/3/myself":
                    auth = self.headers.get("Authorization", "")
                    if auth.startswith("Basic "):
                        try:
                            user, _, token = base64.b64decode(auth[6:]).decode().partition(":")
                        except Exception:
                            user, token = "", ""
                        if fake.jira_accounts.get(user) == token and token:
                            return self._send(200, {"emailAddress": user, "displayName": "Fake User"})
                    return self._send(401, {"errorMessages": ["Client must be authenticated"]})
                if path == "/api/v3/toolkits":
                    if self.headers.get("x-api-key", "") in fake.composio_keys:
                        return self._send(200, {"items": [{"slug": "github"}]})
                    return self._send(401, {"error": "invalid api key"})
                if path == "/sandboxes":
                    if self.headers.get("X-API-Key", "") in fake.e2b_keys:
                        return self._send(200, [])
                    return self._send(401, {"message": "Invalid API key"})
                if path == "/auth.test":
                    if self._bearer() in fake.slack_tokens:
                        return self._send(200, {"ok": True, "team": "fake-team"})
                    return self._send(200, {"ok": False, "error": "invalid_auth"})
                if path == "/api/ingest":
                    if self._bearer() in fake.status_tokens:
                        return self._send(200, {"ok": True})
                    return self._send(401, {"error": "unauthorized"})
                if path == "/mcp":
                    if self._bearer() not in fake.mcp_tokens:
                        return self._send(401, {"error": {"message": "HTTP 401: invalid bearer"}})
                    rpc_method = body.get("method")
                    if rpc_method == "initialize":
                        return self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {
                            "protocolVersion": "2025-06-18", "capabilities": {},
                            "serverInfo": {"name": "fake-mcp", "version": "1"},
                        }})
                    if rpc_method == "tools/list":
                        return self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {
                            "tools": [{"name": name, "description": name,
                                       "inputSchema": {"type": "object", "properties": {}}}
                                      for name in fake.mcp_tools]}})
                    if rpc_method:
                        return self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
                    return self._send(202, b"", "text/plain")
                return self._send(404, {"error": f"no route for {path}"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "FakeServices":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def mcp_url(self) -> str:
        return self.base + "/mcp"
