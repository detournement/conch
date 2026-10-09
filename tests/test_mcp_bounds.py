"""MCP discovery is bounded and reports per-server status.

Observed before these bounds: a ``qdrant`` HTTP server on a LAN address
that was not on the current network made ``HttpMcpClient.list_tools``
wait 30s for ``initialize`` and, because the error path fell through,
another 30s for ``tools/list`` — 60s per dead server, past the shell's
30s startup wait, so every turn ran with no tools at all. A stdio server
that never answered blocked ``readline()`` forever.

No network: HTTP fakes patch ``urllib.request.urlopen``; the one real
socket test listens on 127.0.0.1 and never answers; stdio fixtures are
``sys.executable`` one-liners.
"""

import io
import json
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

from conch import mcp as mcp_mod
from conch.mcp import (
    HttpMcpClient,
    ServerStatus,
    StdioMcpClient,
    ToolDiscovery,
    collect_tools,
    is_transport_error,
    server_statuses,
)


def _tool(name):
    return {"name": name, "description": "", "inputSchema": {"type": "object", "properties": {}}}


class _Response(io.BytesIO):
    def __init__(self, body: dict):
        super().__init__(json.dumps(body).encode())
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Urlopen:
    """Routes fake ``urlopen`` calls by host: ``dead`` hosts raise the
    socket timeout urllib raises for a dropped SYN; others answer plain
    JSON-RPC. Records every (host, method, timeout)."""

    def __init__(self, dead=(), tools=None):
        self.dead = set(dead)
        self.tools = tools or {}
        self.calls = []

    def __call__(self, req, timeout=None):
        host = req.host.split(":")[0]
        payload = json.loads(req.data.decode())
        method = payload.get("method")
        self.calls.append((host, method, timeout))
        if host in self.dead:
            raise socket.timeout("timed out")
        if method == "initialize":
            body = {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "serverInfo": {"name": host}}}
        elif method == "notifications/initialized":
            body = {}
        elif method == "tools/list":
            body = {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"tools": self.tools.get(host, [_tool("ping")])}}
        else:
            body = {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"content": [{"type": "text", "text": "pong"}]}}
        return _Response(body)


class TestHttpHandshakeBound(unittest.TestCase):
    def test_initialize_timeout_marks_unreachable_and_sends_nothing_else(self):
        fake = _Urlopen(dead={"dead.lan"})
        with patch("urllib.request.urlopen", fake):
            client = HttpMcpClient("qdrant", "http://dead.lan:6333/mcp", connect_timeout=5)
            self.assertEqual(client.list_tools(), [])
        self.assertTrue(client.unreachable)
        self.assertEqual(client.last_error, "timed out after 5s")
        # One POST, and it was the handshake: no fall-through to tools/list.
        self.assertEqual([m for _, m, _ in fake.calls], ["initialize"])
        self.assertEqual(fake.calls[0][2], 5)

    def test_handshake_uses_connect_timeout_and_requests_use_the_request_timeout(self):
        fake = _Urlopen()
        with patch("urllib.request.urlopen", fake):
            client = HttpMcpClient("docs", "http://live.lan/mcp", connect_timeout=3, timeout=20)
            client.list_tools()
            client.call_tool("ping", {})
        timeouts = {method: timeout for _, method, timeout in fake.calls}
        self.assertEqual(timeouts["initialize"], 3)
        self.assertEqual(timeouts["tools/list"], 20)
        self.assertEqual(timeouts["tools/call"], 20)

    def test_defaults_are_five_and_thirty_seconds(self):
        client = HttpMcpClient("x", "http://h/mcp")
        self.assertEqual(client.connect_timeout, 5.0)
        self.assertEqual(client.timeout, 30.0)

    def test_connection_refused_is_unreachable_too(self):
        import urllib.error

        err = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        with patch("urllib.request.urlopen", side_effect=err):
            client = HttpMcpClient("x", "http://127.0.0.1:1/mcp")
            self.assertEqual(client.list_tools(), [])
        self.assertTrue(client.unreachable)
        self.assertIn("Connection refused", client.last_error)

    def test_an_unreachable_server_retries_the_handshake_when_called_again(self):
        """The host coming back does not need a /reload for tool calls."""
        fake = _Urlopen(dead={"flaky.lan"})
        with patch("urllib.request.urlopen", fake):
            client = HttpMcpClient("flaky", "http://flaky.lan/mcp")
            self.assertEqual(client.list_tools(), [])
            fake.dead.clear()
            tools = client.list_tools()
        self.assertEqual([t["function"]["name"] for t in tools], ["ping"])
        self.assertFalse(client.unreachable)
        self.assertEqual(client.last_error, "")
        self.assertEqual(
            [m for _, m, _ in fake.calls],
            ["initialize", "initialize", "notifications/initialized", "tools/list"],
        )

    def test_a_server_that_answers_initialize_with_an_error_is_still_used(self):
        """Plain JSON-RPC servers reject initialize; that is not 'unreachable'."""
        def plain(req, timeout=None):
            payload = json.loads(req.data.decode())
            if payload.get("method") == "initialize":
                return _Response({"jsonrpc": "2.0", "id": payload["id"],
                                  "error": {"code": -32601, "message": "Method not found"}})
            return _Response({"jsonrpc": "2.0", "id": payload["id"],
                              "result": {"tools": [_tool("legacy")]}})

        with patch("urllib.request.urlopen", plain):
            client = HttpMcpClient("legacy", "http://old/rpc")
            tools = client.list_tools()
        self.assertEqual([t["function"]["name"] for t in tools], ["legacy"])
        self.assertFalse(client.unreachable)

    def test_tools_list_failure_on_a_live_server_is_an_error_not_unreachable(self):
        def http_404(req, timeout=None):
            import urllib.error
            payload = json.loads(req.data.decode())
            if payload.get("method") == "initialize":
                return _Response({"jsonrpc": "2.0", "id": payload["id"], "result": {}})
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

        with patch("urllib.request.urlopen", http_404):
            client = HttpMcpClient("docs", "http://h/mcp")
            self.assertEqual(client.list_tools(), [])
        self.assertFalse(client.unreachable)
        self.assertIn("HTTP 404", client.last_error)

    def test_real_socket_that_never_answers_times_out_within_the_bound(self):
        """A listening socket that accepts but never replies: the bound is
        on the whole handshake, and the second POST never happens."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        accepted = []

        def accept_and_hold():
            server.settimeout(5)
            try:
                while True:
                    conn, _ = server.accept()
                    accepted.append(conn)
            except (socket.timeout, OSError):
                pass

        thread = threading.Thread(target=accept_and_hold, daemon=True)
        thread.start()
        try:
            client = HttpMcpClient("hang", f"http://127.0.0.1:{port}/mcp", connect_timeout=0.4)
            started = time.monotonic()
            tools = client.list_tools()
            elapsed = time.monotonic() - started
        finally:
            server.close()
            for conn in accepted:
                conn.close()
            thread.join(timeout=6)
        self.assertEqual(tools, [])
        self.assertLess(elapsed, 3.0)
        self.assertTrue(client.unreachable)
        self.assertEqual(client.last_error, "timed out after 0.4s")
        self.assertEqual(len(accepted), 1)


class TestParallelDiscoveryStatus(unittest.TestCase):
    def test_one_dead_server_does_not_cost_the_others_their_tools(self):
        fake = _Urlopen(dead={"dead.lan"}, tools={"live.lan": [_tool("search")]})
        with patch("urllib.request.urlopen", fake):
            clients = {
                "qdrant": HttpMcpClient("qdrant", "http://dead.lan/mcp"),
                "docs": HttpMcpClient("docs", "http://live.lan/mcp"),
            }
            discovery = ToolDiscovery(clients)
            tools, tool_map = discovery.run()
        self.assertEqual([t["function"]["name"] for t in tools], ["search"])
        self.assertIs(tool_map["search"], clients["docs"])
        statuses = discovery.statuses()
        self.assertEqual(statuses["qdrant"].state, "unreachable")
        self.assertEqual(statuses["qdrant"].detail, "timed out after 5s")
        self.assertEqual(statuses["qdrant"].describe(), "unreachable: timed out after 5s")
        self.assertEqual(statuses["docs"].state, "ready")
        self.assertEqual(statuses["docs"].tool_count, 1)
        self.assertTrue(statuses["docs"].describe().startswith("ready (1 tool, "))
        self.assertEqual(discovery.pending(), [])
        # The sync path (daemon, scheduled runs) derives the same picture.
        derived = server_statuses(clients, tool_map)
        self.assertEqual(derived["qdrant"].state, "unreachable")
        self.assertEqual((derived["docs"].state, derived["docs"].tool_count), ("ready", 1))

    def test_statuses_are_readable_while_a_server_is_still_loading(self):
        release = threading.Event()

        class Slow:
            name = "slow"
            last_error = ""
            unreachable = False

            def list_tools(self):
                release.wait(5)
                return [{"type": "function", "function": {"name": "late", "parameters": {}}}]

        class Quick(Slow):
            name = "quick"

            def list_tools(self):
                return [{"type": "function", "function": {"name": "early", "parameters": {}}}]

        discovery = ToolDiscovery({"slow": Slow(), "quick": Quick()})
        result = {}
        runner = threading.Thread(target=lambda: result.update(zip(("tools", "map"), discovery.run())))
        runner.start()
        deadline = time.monotonic() + 5
        while discovery.statuses()["quick"].state != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(discovery.pending(), ["slow"])
        self.assertEqual(discovery.statuses()["slow"].describe(), "loading")
        release.set()
        runner.join(5)
        self.assertEqual([t["function"]["name"] for t in result["tools"]], ["late", "early"])
        self.assertEqual(discovery.pending(), [])

    def test_a_client_that_raises_is_an_error_status_not_a_crash(self):
        class Broken:
            name = "broken"

            def list_tools(self):
                raise RuntimeError("boom")

        discovery = ToolDiscovery({"broken": Broken()})
        self.assertEqual(discovery.run(), ([], {}))
        status = discovery.statuses()["broken"]
        self.assertEqual((status.state, status.detail), ("error", "RuntimeError: boom"))

    def test_collect_tools_keeps_its_two_tuple_contract(self):
        self.assertEqual(collect_tools({}), ([], {}))

    def test_server_status_dataclass_describe(self):
        self.assertEqual(ServerStatus("x").describe(), "loading")
        self.assertEqual(ServerStatus("x", "ready", "", 3, 0.25).describe(), "ready (3 tools, 0.2s)")
        self.assertEqual(ServerStatus("x", "error", "HTTP 500").describe(), "error: HTTP 500")
        self.assertEqual(ServerStatus("x", "unreachable").describe(), "unreachable")


_SILENT_SERVER = "import sys\nfor _ in sys.stdin:\n    pass\n"

_BANNER_SERVER = r"""
import json, sys
sys.stdout.write("starting up, not json\n")
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if req["method"] == "initialize":
        sys.stdout.write("another banner\n")
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}) + "\n")
    elif req["method"] == "tools/list":
        tools = [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"tools": tools}}) + "\n")
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                     "result": {"content": [{"type": "text", "text": "echoed"}]}}) + "\n")
    sys.stdout.flush()
"""


class TestStdioReadTimeout(unittest.TestCase):
    def test_default_read_timeout_is_sixty_seconds(self):
        client = StdioMcpClient("silent", sys.executable, ["-c", _SILENT_SERVER])
        self.addCleanup(client.close)
        self.assertEqual(client.read_timeout, 60.0)

    def test_server_that_never_answers_times_out_instead_of_hanging(self):
        client = StdioMcpClient("silent", sys.executable, ["-c", _SILENT_SERVER], read_timeout=0.3)
        self.addCleanup(client.close)
        started = time.monotonic()
        tools = client.list_tools()
        elapsed = time.monotonic() - started
        self.assertEqual(tools, [])
        self.assertLess(elapsed, 3.0)
        self.assertTrue(client.unreachable)
        self.assertEqual(client.last_error, "no reply to initialize within 0.3s")
        # The handshake failed: tools/list was never sent, and a tool call
        # answers immediately with the reason instead of waiting again.
        started = time.monotonic()
        result = client.call_tool("x", {})
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertIn("unreachable: no reply to initialize", result["content"][0]["text"])

    def test_banners_and_replies_written_together_are_all_read(self):
        client = StdioMcpClient("banner", sys.executable, ["-c", _BANNER_SERVER], read_timeout=10)
        self.addCleanup(client.close)
        tools = client.list_tools()
        self.assertEqual([t["function"]["name"] for t in tools], ["echo"])
        self.assertEqual(client.call_tool("echo", {})["content"][0]["text"], "echoed")
        self.assertFalse(client.unreachable)
        self.assertEqual(client.last_error, "")

    def test_process_that_exits_is_closed_connection_with_exit_code(self):
        client = StdioMcpClient("quitter", sys.executable, ["-c", "import sys; sys.exit(3)"], read_timeout=5)
        self.addCleanup(client.close)
        self.assertEqual(client.list_tools(), [])
        self.assertTrue(client.unreachable)
        self.assertIn("MCP server closed connection", client.last_error)
        self.assertIn("exit code 3", client.last_error)

    def test_command_that_cannot_start_is_unreachable_not_an_exception(self):
        client = StdioMcpClient("missing", "/nonexistent/conch-mcp-server-binary")
        self.assertTrue(client.unreachable)
        self.assertIn("cannot start /nonexistent/conch-mcp-server-binary", client.last_error)
        self.assertEqual(client.list_tools(), [])
        self.assertTrue(is_transport_error(client._send("tools/list")))
        client.close()  # no process: a no-op, not an AttributeError


class TestMcpJsonTimeoutOptions(unittest.TestCase):
    def _clients(self, servers):
        with patch.object(mcp_mod, "_load_config", return_value={"mcpServers": servers}):
            return mcp_mod.create_clients()

    def test_per_server_timeouts_are_read_from_mcp_json(self):
        clients = self._clients({
            "docs": {"type": "http", "url": "http://h/mcp", "connect_timeout": 2, "timeout": "12"},
            "fs": {"command": sys.executable, "args": ["-c", _SILENT_SERVER], "timeout": 7.5},
        })
        self.addCleanup(mcp_mod.close_all, clients)
        self.assertEqual((clients["docs"].connect_timeout, clients["docs"].timeout), (2.0, 12.0))
        self.assertEqual(clients["fs"].read_timeout, 7.5)

    def test_invalid_timeout_fails_loud_naming_the_server(self):
        with self.assertRaises(ValueError) as caught:
            self._clients({"docs": {"type": "http", "url": "http://h/mcp", "connect_timeout": "soon"}})
        self.assertIn("'docs'", str(caught.exception))
        self.assertIn("connect_timeout", str(caught.exception))
        with self.assertRaises(ValueError):
            self._clients({"docs": {"type": "http", "url": "http://h/mcp", "timeout": 0}})

    def test_configured_server_names_needs_no_process(self):
        servers = {
            "fs": {"command": "/definitely/not/installed"},
            "docs": {"type": "http", "url": "http://h/mcp"},
            "skipped": {"type": "http"},
            "junk": "not a block",
        }
        with patch.object(mcp_mod, "_load_config", return_value={"mcpServers": servers}):
            self.assertEqual(mcp_mod.configured_server_names({}), ["fs", "docs"])
            self.assertEqual(
                mcp_mod.configured_server_names({"capitol_docs_url": "http://d/mcp"}),
                ["fs", "docs", "capitol-docs"],
            )


if __name__ == "__main__":
    unittest.main()
