"""Tests for backend health/preflight (plan 3.3)."""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from conch import providers
from conch.providers import check_ollama_health, error_response
from conch.runtime import chat_turn


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestCheckOllamaHealth(unittest.TestCase):
    def setUp(self):
        providers._ollama_tags_cache.clear()

    def test_healthy_server(self):
        def side_effect(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if url.endswith("/api/tags"):
                return _FakeHTTPResponse({"models": [{"name": "qwen3:latest"}]})
            raise AssertionError(url)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertTrue(check_ollama_health({}))

    def test_dead_server(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            self.assertFalse(check_ollama_health({}))

    def test_bypasses_stale_ok_cache(self):
        # A cached "healthy" answer must not mask a server that just died.
        def alive(req, timeout=None):
            return _FakeHTTPResponse({"models": [{"name": "m"}]})

        with patch("urllib.request.urlopen", side_effect=alive):
            self.assertTrue(check_ollama_health({}))
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            self.assertFalse(check_ollama_health({}))


class TestFailureSignaledToCaller(unittest.TestCase):
    def test_usage_carries_error_after_exhausted_fallbacks(self):
        def raw_fn(config, messages, tools):
            return error_response("<urlopen error [Errno 61] Connection refused>")

        with patch("conch.providers.get_fallback_chain", return_value=[]), \
             patch("conch.runtime.time.sleep"), \
             patch("sys.stderr", io.StringIO()):
            reply, usage = chat_turn(
                config={"provider": "ollama", "chat_model": "qwen3"},
                provider="ollama", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_map={}, builtin_clients={},
                max_tool_rounds=2,
            )
        self.assertEqual(reply, "")
        self.assertIn("Connection refused", usage.get("error", ""))

    def test_no_error_on_success(self):
        def raw_fn(config, messages, tools):
            return {"content": "hello", "tool_calls": None,
                    "_usage": {"input_tokens": 1, "output_tokens": 1},
                    "_model": "m"}

        with patch("sys.stderr", io.StringIO()):
            reply, usage = chat_turn(
                config={}, provider="ollama", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_map={}, builtin_clients={},
                max_tool_rounds=2,
            )
        self.assertEqual(reply, "hello")
        self.assertNotIn("error", usage)


if __name__ == "__main__":
    unittest.main()
