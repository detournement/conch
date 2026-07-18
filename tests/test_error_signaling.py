"""Tests for unified provider error signaling (plan 0.3).

All providers (including Ollama) fail with a single ``[API error: ...]``
content prefix plus a structured ``_error`` flag; transient detection covers
connection-refused/timeout/404 as well as rate limits and 5xx; and error text
is never returned as a chat reply, appended to history, or saved to memory.
"""

import io
import json
import sys
import unittest
import urllib.error
from unittest.mock import patch

from conch.providers import error_response, raw_ollama
from conch.runtime import (
    chat_turn,
    error_detail,
    is_connection_error,
    is_error_response,
    is_structural_error,
    is_transient_error,
)


class TestErrorResponseShape(unittest.TestCase):
    def test_prefix_flag_and_no_tool_calls(self):
        resp = error_response("connection refused")
        self.assertEqual(resp["content"], "[API error: connection refused]")
        self.assertTrue(resp["_error"])
        self.assertIsNone(resp["tool_calls"])

    def test_detected_by_flag(self):
        self.assertTrue(is_error_response({"_error": True, "content": "x"}))

    def test_detected_by_prefix(self):
        self.assertTrue(is_error_response({"content": "[API error: boom]"}))

    def test_normal_reply_not_error(self):
        self.assertFalse(is_error_response({"content": "all good"}))
        self.assertFalse(is_error_response({"content": ""}))

    def test_error_detail_strips_prefix(self):
        self.assertEqual(error_detail(error_response("boom")), "boom")

    def test_error_detail_passthrough_without_prefix(self):
        self.assertEqual(error_detail({"content": "raw failure"}), "raw failure")


class TestErrorClassification(unittest.TestCase):
    def test_connection_failures_transient(self):
        for msg in (
            "<urlopen error [Errno 61] Connection refused>",
            "The read operation timed out",
            "HTTP Error 404: Not Found",
            "server unreachable",
        ):
            self.assertTrue(is_transient_error(msg), msg)

    def test_rate_limits_and_5xx_transient(self):
        for msg in ("HTTP Error 429", "HTTP Error 503", "Overloaded"):
            self.assertTrue(is_transient_error(msg), msg)

    def test_structural_not_transient(self):
        self.assertFalse(is_transient_error("invalid_request_error: bad schema"))

    def test_structural_detection(self):
        self.assertTrue(is_structural_error("authentication_error: invalid api key"))
        self.assertFalse(is_structural_error("HTTP Error 500"))

    def test_connection_error_detection(self):
        self.assertTrue(is_connection_error("<urlopen error [Errno 61] Connection refused>"))
        self.assertFalse(is_connection_error("HTTP Error 429"))


class TestRawOllamaUnifiedErrors(unittest.TestCase):
    def test_dead_server_uses_api_error_prefix(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("[Errno 61] Connection refused"),
        ):
            result = raw_ollama({"provider": "ollama", "model": "qwen3"}, [])
        self.assertTrue(result.get("_error"))
        self.assertTrue(result["content"].startswith("[API error:"))
        self.assertNotIn("Ollama error", result["content"])


class _QuietStderr(unittest.TestCase):
    def setUp(self):
        self._stderr = io.StringIO()
        patcher = patch("sys.stderr", self._stderr)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No real sleeping during transient retries
        sleep_patcher = patch("conch.runtime.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)


class TestChatTurnNeverPersistsErrors(_QuietStderr):
    def _run(self, raw_fn, provider="ollama", messages=None):
        messages = messages if messages is not None else [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        with patch("conch.providers.get_fallback_chain", return_value=[]):
            reply, usage = chat_turn(
                config={"provider": provider, "chat_model": "qwen3"},
                provider=provider,
                raw_fn=raw_fn,
                messages=messages,
                tools=None,
                tool_map={},
                builtin_clients={},
                max_tool_rounds=3,
            )
        return reply, messages

    def test_dead_server_returns_empty_reply(self):
        def raw_fn(config, messages, tools):
            return error_response("<urlopen error [Errno 61] Connection refused>")

        reply, messages = self._run(raw_fn)
        self.assertEqual(reply, "", "error text must never be the reply")
        self.assertEqual(len(messages), 2, "no error message may be appended to history")

    def test_dead_ollama_prints_unreachable_message(self):
        def raw_fn(config, messages, tools):
            return error_response("<urlopen error [Errno 61] Connection refused>")

        self._run(raw_fn, provider="ollama")
        err = self._stderr.getvalue()
        self.assertIn("Ollama server unreachable at", err)
        self.assertIn("11434", err)

    def test_transient_error_retried_then_succeeds(self):
        responses = [
            error_response("HTTP Error 503"),
            {"role": "assistant", "content": "recovered", "tool_calls": None,
             "_usage": {"input_tokens": 1, "output_tokens": 1}, "_model": "qwen3"},
        ]

        def raw_fn(config, messages, tools):
            return responses.pop(0)

        reply, messages = self._run(raw_fn)
        self.assertEqual(reply, "recovered")

    def test_structural_error_skips_same_provider_fallbacks(self):
        calls = []

        def raw_fn(config, messages, tools):
            calls.append(1)
            return error_response("invalid_request_error: bad tool schema")

        reply, _ = self._run(raw_fn)
        self.assertEqual(reply, "")
        # structural → no transient retry, no same-provider fallback
        self.assertEqual(len(calls), 1)


class TestSummarizeNeverSavesErrors(unittest.TestCase):
    def test_error_summary_not_saved(self):
        from conch.app import _summarize_and_save

        class FakeMemory:
            def __init__(self):
                self.saved = []

            def add(self, content, source=""):
                self.saved.append(content)

        memory = FakeMemory()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "two"},
        ]

        def raw_fn(config, msgs, tools):
            return error_response("<urlopen error [Errno 61] Connection refused>")

        _summarize_and_save(messages, {"provider": "ollama"}, raw_fn, memory)
        self.assertEqual(memory.saved, [], "provider errors must never become memories")


if __name__ == "__main__":
    unittest.main()
