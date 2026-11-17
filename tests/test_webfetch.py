"""web_fetch against a local http.server stub: HTML reduced to readable
text, JSON pretty-printed, binary refused, URL guards (scheme, embedded
credentials), and the prompt_all approval gate."""

import contextlib
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from conch.seats import _strict_parameters
from conch.tooling import LocalShellPolicy, PermissionState
from conch.webfetch import WEB_FETCH_TOOL, WebFetchClient, html_to_text

PAGE = b"""<!doctype html><html><head><title>Stub  Page</title>
<style>body { color: red }</style><script>var leaked = "SCRIPT_BODY";</script>
</head><body><h1>Welcome</h1><p>Read the <a href="/docs/intro">intro docs</a> now.</p>
<noscript>NOSCRIPT_BODY</noscript><ul><li>one</li><li>two</li></ul></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Handler.hits.append((self.path, self.headers.get("User-Agent")))
        if self.path == "/page":
            self._send(200, "text/html; charset=utf-8", PAGE)
        elif self.path == "/data":
            self._send(200, "application/json", b'{"b": [1, 2], "a": "x"}')
        elif self.path == "/image":
            self._send(200, "image/png", b"\x89PNG\r\n\x1a\n\x00\x00")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
        else:
            self._send(404, "text/plain", b"no such thing here")


class WebFetchCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.hits = []
        env = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})
        env.start()
        self.addCleanup(env.stop)

    def client(self, mode="safe_auto", interactive=True, input_fn=None, config=None):
        client = WebFetchClient(permissions=PermissionState(permission_mode=mode),
                                config=config or {})
        client.set_policy(LocalShellPolicy(interactive=interactive, input_fn=input_fn))
        return client

    def fetch(self, client, url, **extra):
        with contextlib.redirect_stdout(io.StringIO()):
            result = client.call_tool("web_fetch", dict(url=url, **extra))
        return result["content"][0]["text"], bool(result.get("isError"))


class TestContent(WebFetchCase):
    def test_html_becomes_readable_text(self):
        text, error = self.fetch(self.client(), self.base + "/page")
        self.assertFalse(error, text)
        self.assertIn(f"URL: {self.base}/page", text)
        self.assertIn("Status: 200", text)
        self.assertIn("Content-Type: text/html", text)
        body = text.split("\n\n", 1)[1]
        self.assertTrue(body.startswith("Title: Stub Page"), body)
        self.assertIn("# Welcome", body)
        self.assertIn(f"intro docs ({self.base}/docs/intro)", body)
        for dropped in ("SCRIPT_BODY", "NOSCRIPT_BODY", "color: red"):
            self.assertNotIn(dropped, body)
        self.assertIn("- one\n- two", body)

    def test_user_agent_identifies_conch(self):
        self.fetch(self.client(), self.base + "/page")
        self.assertTrue(_Handler.hits[0][1].startswith("conch/"))

    def test_json_pretty_printed(self):
        text, _ = self.fetch(self.client(), self.base + "/data")
        body = text.split("\n\n", 1)[1]
        self.assertEqual(json.loads(body), {"b": [1, 2], "a": "x"})
        self.assertIn('\n  "b": [', body)

    def test_binary_content_type_refused(self):
        text, error = self.fetch(self.client(), self.base + "/image")
        self.assertTrue(error)
        self.assertIn("image/png", text)
        self.assertNotIn("PNG", text.split("\n\n", 1)[1].replace("image/png", ""))

    def test_http_error_returns_status_and_excerpt(self):
        text, error = self.fetch(self.client(), self.base + "/missing")
        self.assertTrue(error)
        self.assertIn("Status: 404", text)
        self.assertIn("no such thing here", text)

    def test_redirect_followed(self):
        text, _ = self.fetch(self.client(), self.base + "/redirect")
        self.assertIn(f"URL: {self.base}/page", text)
        self.assertIn("Welcome", text)

    def test_max_chars_truncates(self):
        text, _ = self.fetch(self.client(), self.base + "/page", max_chars=40)
        body = text.split("\n\n", 1)[1]
        self.assertLessEqual(len(body), 40)


class TestGuards(WebFetchCase):
    def test_non_http_schemes_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"):
            text, error = self.fetch(self.client(mode="yolo"), url)
            self.assertTrue(error, url)
            self.assertIn("Refused", text)
        self.assertEqual(_Handler.hits, [])

    def test_credentials_in_url_refused(self):
        # Synthetic values, split so secret scanners don't flag the fixture.
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        api_key = "Zx9QwErTy" + "1234"
        port = self.server.server_address[1]
        for url in (
            f"http://user:hunter2@127.0.0.1:{port}/page",
            f"{self.base}/page?leak={token}",
            f"{self.base}/page?api_key={api_key}",
        ):
            text, error = self.fetch(self.client(mode="yolo"), url)
            self.assertTrue(error, url)
            self.assertNotIn(token, text)
        self.assertEqual(_Handler.hits, [])


class TestApproval(WebFetchCase):
    def test_prompt_all_decline(self):
        answers = iter(["n", "not that site"])
        text, _ = self.fetch(
            self.client(mode="prompt_all", input_fn=lambda prompt: next(answers)),
            self.base + "/page",
        )
        self.assertIn("declined", text)
        self.assertIn("not that site", text)
        self.assertEqual(_Handler.hits, [])

    def test_prompt_all_eof_declines(self):
        def eof(prompt):
            raise EOFError

        text, _ = self.fetch(self.client(mode="prompt_all", input_fn=eof),
                             self.base + "/page")
        self.assertIn("nothing was fetched", text)
        self.assertEqual(_Handler.hits, [])

    def test_always_allows_host_for_session(self):
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return "a"

        client = self.client(mode="prompt_all", input_fn=answer)
        self.fetch(client, self.base + "/page")
        self.fetch(client, self.base + "/data")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(_Handler.hits), 2)

    def test_non_interactive_prompt_all_refuses(self):
        text, _ = self.fetch(self.client(mode="prompt_all", interactive=False),
                             self.base + "/page")
        self.assertIn("Refused", text)
        self.assertEqual(_Handler.hits, [])

    def test_allow_domains_config_skips_prompt(self):
        client = self.client(mode="prompt_all", interactive=False,
                             config={"web_fetch_allow_domains": "example.org, 127.0.0.1"})
        text, error = self.fetch(client, self.base + "/page")
        self.assertFalse(error, text)
        self.assertEqual(len(_Handler.hits), 1)

    def test_remote_variant_never_prompts(self):
        def boom(prompt):
            raise AssertionError("remote variant prompted")

        host = self.client(mode="yolo", input_fn=boom)
        text, _ = self.fetch(host.remote_variant(), self.base + "/page")
        self.assertIn("Refused", text)


class TestHelpers(unittest.TestCase):
    def test_schema_fits_strict_seats(self):
        self.assertIsNotNone(_strict_parameters(WEB_FETCH_TOOL["function"]["parameters"]))

    def test_relative_links_resolve_against_base(self):
        text = html_to_text(
            '<base href="https://ex.com/a/"><p><a href="b">B</a> and '
            '<a href="#top">top</a></p><pre>  keep\n    indent</pre>',
            "https://ignored.example/",
        )
        self.assertIn("B (https://ex.com/a/b)", text)
        self.assertIn("top", text)
        self.assertNotIn("#top", text)
        self.assertIn("  keep\n    indent", text)


if __name__ == "__main__":
    unittest.main()
