"""Keep-alive transport for provider calls (conch.http_transport)."""

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from conch import http_transport, providers


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *args):
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, code, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        body = self._body()
        with self.server.lock:
            self.server.requests.append((self.path, dict(self.headers), body))
        if self.path == "/ok":
            self._send(200, {"ok": True})
        elif self.path == "/slow":
            time.sleep(1.0)
            self._send(200, {"ok": True})
        elif self.path == "/bad":
            self._send(400, {"error": {"message": "bad request"}})
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event in (b'data: {"n": 1}\n\n', b'data: {"n": 2}\n\n', b"data: [DONE]\n\n"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/once":
            # Advertises keep-alive, then drops the socket while idle.
            self._send(200, {"ok": True})
            self.wfile.flush()
            self.close_connection = True
        elif self.path == "/partial":
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"a\"")
            self.wfile.flush()
            self.close_connection = True
        elif self.path == "/halfstatus":
            # A few status-line bytes, then a hard reset.
            self.wfile.write(b"HTTP/1.1 2")
            self.wfile.flush()
            import struct
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                       struct.pack("ii", 1, 0))
            self.close_connection = True
        elif self.path == "/chat/completions":
            self._send(200, {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })
        else:
            self._send(404, {"error": "missing"})


class TransportServerTest(unittest.TestCase):
    def setUp(self):
        http_transport.clear_pool()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.lock = threading.Lock()
        self.server.connections = 0
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        http_transport.clear_pool()
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, timeout=5, config=None):
        req = urllib.request.Request(
            self.base + path, data=b'{"x": 1}',
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return http_transport.urlopen(req, timeout=timeout, config=config)

    def test_sequential_requests_share_one_connection(self):
        for _ in range(5):
            with self._post("/ok") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getcode(), 200)
                self.assertEqual(json.loads(response.read()), {"ok": True})
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(len(self.server.requests), 5)
        headers = self.server.requests[0][1]
        self.assertNotEqual(headers.get("Connection", "").lower(), "close")
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertTrue(headers.get("User-Agent", "").startswith("Python-urllib/"))

    def test_string_url_get(self):
        with http_transport.urlopen(self.base + "/ok", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
        with http_transport.urlopen(self.base + "/ok", timeout=5) as response:
            response.read()
        self.assertEqual(self.server.connections, 1)

    def test_streamed_sse_releases_connection_after_done(self):
        for _ in range(3):
            with self._post("/sse") as response:
                chunks = list(providers._iter_sse(response))
            self.assertEqual(chunks, [{"n": 1}, {"n": 2}])
        self.assertEqual(self.server.connections, 1)

    def test_open_stream_holds_its_connection(self):
        stream = self._post("/sse")
        stream.readline()
        with self._post("/ok") as response:
            response.read()
        stream.close()
        self.assertEqual(self.server.connections, 2)

    def test_http_error_semantics_and_reuse(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/bad")
        exc = caught.exception
        self.assertEqual(exc.code, 400)
        self.assertEqual(exc.headers.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(exc.read()), {"error": {"message": "bad request"}})
        exc.close()
        with self._post("/ok") as response:
            response.read()
        self.assertEqual(self.server.connections, 1)

    def test_redirect_follows_stock_urllib_rules(self):
        with http_transport.urlopen(self.base + "/redirect", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})
            self.assertTrue(response.geturl().endswith("/ok"))

    def test_opt_out_uses_stock_urllib(self):
        for _ in range(2):
            with self._post("/ok", config={"http_keepalive": "false"}) as response:
                response.read()
        self.assertEqual(self.server.connections, 2)
        self.assertEqual(self.server.requests[0][1].get("Connection"), "close")

    def test_proxy_falls_back_to_stock_path(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://proxy.invalid:1"}), \
                mock.patch("urllib.request.proxy_bypass", return_value=False), \
                mock.patch.object(http_transport, "_STOCK_URLOPEN",
                                  urllib.request.urlopen), \
                mock.patch.object(http_transport, "_delegate",
                                  return_value="stock") as delegate:
            self.assertEqual(self._post("/ok"), "stock")
        delegate.assert_called_once()

    def test_stale_connection_retried_once_on_fresh_connection(self):
        with self._post("/once") as response:
            response.read()
        time.sleep(0.1)  # let the server close its end
        # Force reuse of the dead socket to exercise the send/receive race.
        with mock.patch.object(http_transport, "_is_alive", return_value=True):
            with self._post("/ok") as response:
                self.assertEqual(json.loads(response.read()), {"ok": True})
        self.assertEqual(self.server.connections, 2)
        self.assertGreaterEqual(http_transport.pool_stats()["stale_retries"], 1)

    def test_dead_idle_connection_is_not_reused(self):
        with self._post("/once") as response:
            response.read()
        time.sleep(0.1)
        with self._post("/ok") as response:
            response.read()
        self.assertEqual(self.server.connections, 2)

    def test_no_retry_after_response_bytes(self):
        with self.assertRaises(Exception):
            with self._post("/partial") as response:
                response.read()
        self.assertEqual(len(self.server.requests), 1)

    def test_reused_connection_not_retried_after_partial_status(self):
        with self._post("/ok") as response:
            response.read()
        with self.assertRaises(Exception):
            with self._post("/halfstatus") as response:
                response.read()
        paths = [path for path, _, _ in self.server.requests]
        self.assertEqual(paths, ["/ok", "/halfstatus"])
        self.assertEqual(self.server.connections, 1)

    def test_read_timeout(self):
        started = time.monotonic()
        with self.assertRaises((socket.timeout, urllib.error.URLError, OSError)):
            self._post("/slow", timeout=0.2)
        self.assertLess(time.monotonic() - started, 0.9)

    def test_connect_error_is_urlerror(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(urllib.error.URLError):
            http_transport.urlopen("http://127.0.0.1:%d/ok" % port, timeout=2)

    def test_concurrent_requests_are_isolated_and_pool_is_bounded(self):
        errors = []

        def worker():
            try:
                for _ in range(10):
                    with self._post("/ok") as response:
                        assert json.loads(response.read()) == {"ok": True}
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.server.requests), 120)
        self.assertLessEqual(self.server.connections, 12)
        self.assertLessEqual(http_transport.pool_stats()["idle"],
                             http_transport.MAX_IDLE_PER_HOST)

    def test_idle_connections_expire(self):
        with self._post("/ok") as response:
            response.read()
        with mock.patch.object(http_transport, "IDLE_TIMEOUT", 0.0):
            time.sleep(0.01)
            with self._post("/ok") as response:
                response.read()
        self.assertEqual(self.server.connections, 2)

    def test_raw_custom_provider_reuses_connection(self):
        config = {"custom_base_url": self.base, "chat_model": "m"}
        with mock.patch.object(providers, "validate_custom_model", return_value=(True, "")), \
                mock.patch.object(providers, "get_custom_context_window", return_value=8192):
            for _ in range(3):
                result = providers.raw_custom(config, [{"role": "user", "content": "hi"}])
                self.assertEqual(result.get("content"), "hi", result)
        self.assertEqual(self.server.connections, 1)


class TransportMockCompatibilityTest(unittest.TestCase):
    def test_patched_urlopen_intercepts_provider_calls(self):
        fake = mock.MagicMock()
        fake.return_value.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "mocked"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}), \
                mock.patch("urllib.request.urlopen", fake):
            result = providers.raw_openai({"chat_model": "gpt-5.4-mini"},
                                          [{"role": "user", "content": "hi"}])
        self.assertIn("mocked", json.dumps(result))
        fake.assert_called_once()
        self.assertEqual(fake.call_args.kwargs, {"timeout": 60})
        self.assertIsInstance(fake.call_args.args[0], urllib.request.Request)

    def test_keepalive_flag_parsing(self):
        self.assertTrue(http_transport.keepalive_enabled(None))
        self.assertTrue(http_transport.keepalive_enabled({}))
        self.assertTrue(http_transport.keepalive_enabled({"http_keepalive": "true"}))
        for value in ("false", "False", "0", "no", "off", False):
            self.assertFalse(http_transport.keepalive_enabled({"http_keepalive": value}))


if __name__ == "__main__":
    unittest.main()
