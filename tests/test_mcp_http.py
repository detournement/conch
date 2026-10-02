"""HttpMcpClient speaks MCP Streamable HTTP: initialize → session id →
initialized notification, and recovers once when the session is gone.

The fake server below behaves like FastMCP's streamable-http transport
(the Capitol docs server, ENG-5629): every non-initialize request needs
an ``mcp-session-id`` header, responses are SSE, notifications get an
empty 202. A second fake answers plain JSON without sessions, which is
how the pre-handshake client was used and must keep working.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from conch.mcp import HttpMcpClient, _parse_sse


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict, status: int = 200):
        super().__init__(body)
        self.headers = _Headers(headers)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Headers(dict):
    def get(self, key, default=None):  # urllib headers are case-insensitive
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default

    def items(self):
        return dict.items(self)


def _sse(message: dict) -> bytes:
    return ("event: message\ndata: " + json.dumps(message) + "\n\n").encode()


class FakeStreamableServer:
    """FastMCP-shaped: sessions required, SSE bodies, 202 for notifications."""

    def __init__(self, tools=None):
        self.tools = tools or [
            {"name": "search_capitol_docs", "description": "search",
             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}
        ]
        self.sessions = set()
        self.next_session = 1
        self.requests = []  # (method, session header or None)
        self.calls = []

    def __call__(self, req, timeout=0):
        payload = json.loads(req.data.decode())
        session = req.get_header("Mcp-session-id")
        method = payload.get("method")
        self.requests.append((method, session))
        if method == "initialize":
            sid = f"sess-{self.next_session}"
            self.next_session += 1
            self.sessions.add(sid)
            return _Response(
                _sse({"jsonrpc": "2.0", "id": payload["id"],
                      "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "serverInfo": {"name": "capitol-docs-server", "version": "3.4.2"}}}),
                {"Content-Type": "text/event-stream", "mcp-session-id": sid},
            )
        if session not in self.sessions:
            body = json.dumps({"jsonrpc": "2.0", "id": "server-error",
                               "error": {"code": -32600, "message": "Bad Request: Missing session ID"}})
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request",
                                         {"Content-Type": "application/json"},
                                         io.BytesIO(body.encode()))
        if method == "notifications/initialized":
            return _Response(b"", {"Content-Type": "text/plain"}, status=202)
        if method == "tools/list":
            return _Response(
                _sse({"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": self.tools}}),
                {"Content-Type": "text/event-stream"},
            )
        if method == "tools/call":
            self.calls.append(payload["params"])
            # A progress notification precedes the real response in the stream.
            body = _sse({"jsonrpc": "2.0", "method": "notifications/message",
                         "params": {"level": "info", "data": "working"}}) + _sse(
                {"jsonrpc": "2.0", "id": payload["id"],
                 "result": {"content": [{"type": "text", "text": "doc body"}]}})
            return _Response(body, {"Content-Type": "text/event-stream"})
        raise AssertionError(f"unexpected method {method}")


class FakePlainServer:
    """Pre-spec JSON-RPC-over-HTTP server: no initialize, no sessions."""

    def __init__(self):
        self.methods = []

    def __call__(self, req, timeout=0):
        payload = json.loads(req.data.decode())
        method = payload.get("method")
        self.methods.append(method)
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": payload.get("id"),
                    "error": {"code": -32601, "message": "Method not found"}}
        elif method == "notifications/initialized":
            body = {}
        elif method == "tools/list":
            body = {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"tools": [{"name": "ping", "description": "", "inputSchema": {}}]}}
        else:
            body = {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"content": [{"type": "text", "text": "pong"}]}}
        return _Response(json.dumps(body).encode(), {"Content-Type": "application/json"})


class TestStreamableHttpHandshake(unittest.TestCase):
    def test_first_call_initializes_and_reuses_the_session(self):
        server = FakeStreamableServer()
        with patch("urllib.request.urlopen", side_effect=server):
            client = HttpMcpClient("docs", "http://127.0.0.1:8042/mcp/")
            tools = client.list_tools()
            result = client.call_tool("search_capitol_docs", {"query": "evals"})
        self.assertEqual([t["function"]["name"] for t in tools], ["search_capitol_docs"])
        self.assertEqual(result["content"][0]["text"], "doc body")
        self.assertEqual(
            [m for m, _ in server.requests],
            ["initialize", "notifications/initialized", "tools/list", "tools/call"],
        )
        # Every request after initialize carries the issued session id.
        self.assertEqual({s for m, s in server.requests if m != "initialize"}, {"sess-1"})
        self.assertEqual(client.server_info.get("name"), "capitol-docs-server")

    def test_lost_session_is_renegotiated_once_and_the_call_replayed(self):
        server = FakeStreamableServer()
        with patch("urllib.request.urlopen", side_effect=server):
            client = HttpMcpClient("docs", "http://127.0.0.1:8042/mcp")
            client.list_tools()
            server.sessions.clear()  # the server restarted
            result = client.call_tool("search_capitol_docs", {"query": "x"})
        self.assertEqual(result["content"][0]["text"], "doc body")
        methods = [m for m, _ in server.requests]
        self.assertEqual(methods.count("initialize"), 2)
        self.assertEqual(methods[-1], "tools/call")
        self.assertEqual(server.requests[-1][1], "sess-2")
        self.assertEqual(len(server.calls), 1)

    def test_persistent_session_failure_surfaces_as_tool_error_not_exception(self):
        server = FakeStreamableServer()

        def never_a_session(req, timeout=0):
            payload = json.loads(req.data.decode())
            if payload.get("method") == "initialize":
                # A broken proxy: initialize succeeds but drops the header.
                return _Response(
                    _sse({"jsonrpc": "2.0", "id": payload["id"], "result": {}}),
                    {"Content-Type": "text/event-stream"},
                )
            return server(req, timeout)

        with patch("urllib.request.urlopen", side_effect=never_a_session):
            client = HttpMcpClient("docs", "http://127.0.0.1:8042/mcp")
            result = client.call_tool("search_capitol_docs", {"query": "x"})
        text = result["content"][0]["text"]
        self.assertIn("MCP HTTP error", text)
        self.assertIn("Missing session ID", text)
        # Exactly one retry: two initialize attempts, two tool calls, then stop.
        self.assertEqual([m for m, _ in server.requests].count("tools/call"), 2)

    def test_plain_json_rpc_servers_keep_working_without_sessions(self):
        server = FakePlainServer()
        with patch("urllib.request.urlopen", side_effect=server):
            client = HttpMcpClient("legacy", "http://127.0.0.1:9000/rpc")
            tools = client.list_tools()
            result = client.call_tool("ping", {})
        self.assertEqual(tools[0]["function"]["name"], "ping")
        self.assertEqual(result["content"][0]["text"], "pong")
        self.assertIsNone(client._session_id)
        # initialize was offered once, rejected, and never repeated.
        self.assertEqual(server.methods.count("initialize"), 1)
        self.assertNotIn("notifications/initialized", server.methods)

    def test_extra_headers_are_sent_on_every_request_including_initialize(self):
        server = FakeStreamableServer()
        seen = []

        def spy(req, timeout=0):
            seen.append(req.get_header("Authorization"))
            return server(req, timeout)

        with patch("urllib.request.urlopen", side_effect=spy):
            client = HttpMcpClient("docs", "http://h/mcp", headers={"Authorization": "Bearer t"})
            client.list_tools()
        self.assertEqual(set(seen), {"Bearer t"})
        self.assertEqual(len(seen), 3)

    def test_connection_refused_is_an_error_payload(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            client = HttpMcpClient("docs", "http://127.0.0.1:1/mcp")
            self.assertEqual(client.list_tools(), [])
            result = client.call_tool("x", {})
        self.assertIn("connection refused", result["content"][0]["text"])


class TestSseParsing(unittest.TestCase):
    def test_prefers_the_message_matching_the_request_id(self):
        body = (
            "data: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n"
            "data: {\"jsonrpc\":\"2.0\",\"id\":7,\"result\":{\"ok\":true}}\n\n"
            "data: {\"jsonrpc\":\"2.0\",\"id\":8,\"result\":{\"ok\":false}}\n\n"
        )
        self.assertEqual(_parse_sse(body, 7)["result"], {"ok": True})

    def test_falls_back_to_the_last_parseable_message(self):
        body = "data: not json\n\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":1}\n\n"
        self.assertEqual(_parse_sse(body, 99)["result"], 1)
        self.assertIsNone(_parse_sse("event: ping\n\n", 1))


class TestCapitolDocsConfigKey(unittest.TestCase):
    """``capitol_docs_url`` mounts the docs server as MCP server
    ``capitol-docs`` without an mcp.json block; an explicit block of the
    same name wins; unset means no client at all. ``create_clients()``
    keeps its no-argument signature (tests and plugins stub it)."""

    def _clients(self, mcp_json: dict, config: dict):
        from conch import mcp as mcp_mod

        with patch.object(mcp_mod, "_load_config", return_value=mcp_json):
            return mcp_mod.mount_config_servers(mcp_mod.create_clients(), config)

    def test_unset_key_adds_nothing(self):
        self.assertEqual(self._clients({"mcpServers": {}}, {}), {})
        self.assertEqual(self._clients({"mcpServers": {}}, None), {})
        self.assertEqual(self._clients({"mcpServers": {}}, {"capitol_docs_url": "  "}), {})

    def test_config_key_mounts_an_http_client(self):
        clients = self._clients({"mcpServers": {}},
                                {"capitol_docs_url": "http://127.0.0.1:8042/mcp/"})
        self.assertEqual(list(clients), ["capitol-docs"])
        client = clients["capitol-docs"]
        self.assertIsInstance(client, HttpMcpClient)
        self.assertEqual(client.url, "http://127.0.0.1:8042/mcp")
        self.assertEqual(client.headers, {})

    def test_explicit_mcp_json_entry_takes_precedence(self):
        mcp_json = {"mcpServers": {"capitol-docs": {
            "type": "http", "url": "https://docs.example/mcp",
            "headers": {"X-Org": "acme"}}}}
        clients = self._clients(mcp_json, {"capitol_docs_url": "http://127.0.0.1:8042/mcp"})
        self.assertEqual(list(clients), ["capitol-docs"])
        self.assertEqual(clients["capitol-docs"].url, "https://docs.example/mcp")
        self.assertEqual(clients["capitol-docs"].headers, {"X-Org": "acme"})

    def test_env_alias_maps_to_the_config_key(self):
        from conch.config import ENV_CONFIG_KEYS

        self.assertEqual(ENV_CONFIG_KEYS.get("CONCH_CAPITOL_DOCS_URL"), "capitol_docs_url")

    def test_system_prompt_routes_platform_questions_only_when_mounted(self):
        # Without a skill loaded, a one-shot "how do I connect Slack?" went
        # to conch's own /connect and answers from the docs tools dropped
        # the citation. The guidance rides on the config key, so the
        # prompt stays tiny for everyone without a docs server.
        from conch.prompts import build_self_description, capitol_docs_guidance

        self.assertEqual(capitol_docs_guidance({}), "")
        self.assertEqual(capitol_docs_guidance(None), "")
        self.assertNotIn("corpus_version", build_self_description("anthropic", "claude-sonnet-5", {}))

        text = build_self_description(
            "anthropic", "claude-sonnet-5", {"capitol_docs_url": "http://127.0.0.1:8042/mcp"})
        for anchor in ("search_capitol_docs", "how_do_i", "explain_concept",
                       "never from memory", "doc id(s)", "corpus_version",
                       "not conch's /connect"):
            self.assertIn(anchor, text)


if __name__ == "__main__":
    unittest.main()
