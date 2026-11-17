"""Tests for runtime tool-execution visibility helpers and verbose mode."""

import io
import sys
import unittest
from unittest.mock import patch

from conch.runtime import (
    _print_tool_preview,
    _print_tool_result,
    _summarize_args,
    _summarize_result,
    chat_turn,
)


class TestSummarizeArgs(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_summarize_args({}), "")

    def test_command_argument_special_cased(self):
        result = _summarize_args({"command": "ls -la"})
        self.assertEqual(result, "ls -la")

    def test_long_command_truncated(self):
        long_cmd = "x" * 200
        result = _summarize_args({"command": long_cmd})
        self.assertLessEqual(len(result), 80)
        self.assertTrue(result.endswith("…"))

    def test_command_newlines_collapsed(self):
        result = _summarize_args({"command": "echo a\necho b"})
        self.assertNotIn("\n", result)
        self.assertIn("⏎", result)

    def test_other_args_json_encoded(self):
        result = _summarize_args({"name": "foo", "count": 3})
        self.assertIn("foo", result)
        self.assertIn("3", result)


class TestSummarizeResult(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_summarize_result(""), "(empty)")

    def test_short_unchanged(self):
        self.assertEqual(_summarize_result("hello"), "hello")

    def test_too_many_lines_truncated(self):
        text = "\n".join(f"line{i}" for i in range(20))
        result = _summarize_result(text, max_lines=3)
        self.assertIn("line0", result)
        self.assertIn("line2", result)
        self.assertNotIn("line19", result)
        self.assertIn("17 more", result)

    def test_too_many_chars_truncated(self):
        result = _summarize_result("x" * 1000, max_chars=100)
        self.assertLessEqual(len(result), 101)
        self.assertTrue(result.endswith("…"))


class TestPrintTool(unittest.TestCase):
    def setUp(self):
        self.buf = io.StringIO()
        self._old = sys.stderr
        sys.stderr = self.buf

    def tearDown(self):
        sys.stderr = self._old

    def test_preview_shows_tool_name(self):
        _print_tool_preview("local_shell", {"command": "ls"}, verbose=False)
        self.assertIn("local_shell", self.buf.getvalue())

    def test_preview_verbose_shows_args(self):
        _print_tool_preview("local_shell", {"command": "ls -la /"}, verbose=True)
        out = self.buf.getvalue()
        self.assertIn("local_shell", out)
        self.assertIn("ls -la /", out)

    def test_preview_non_verbose_hides_args(self):
        _print_tool_preview("local_shell", {"command": "ls -la /"}, verbose=False)
        self.assertNotIn("ls -la /", self.buf.getvalue())

    def test_result_hidden_when_not_verbose(self):
        _print_tool_result("hello world", verbose=False)
        self.assertEqual(self.buf.getvalue(), "")

    def test_result_shown_when_verbose(self):
        _print_tool_result("hello world", verbose=True)
        self.assertIn("hello world", self.buf.getvalue())

    def test_error_shown_even_when_not_verbose(self):
        _print_tool_result("Error: kaboom", verbose=False, error=True)
        self.assertIn("kaboom", self.buf.getvalue())


class TestChatTurnToolCancellation(unittest.TestCase):
    def test_keyboard_interrupt_in_tool_becomes_synthetic_result(self):
        """Ctrl+C inside a tool turns into a 'cancelled by user' message,
        and the next round returns the model's reply normally."""

        class CancellingClient:
            def call_tool(self, name, arguments):
                raise KeyboardInterrupt

        responses = [
            {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "local_shell", "arguments": "{\"command\": \"sleep 99\"}"},
                }],
                "_usage": {"input_tokens": 1, "output_tokens": 1},
                "_model": "test-model",
            },
            {
                "content": "Okay, stopping.",
                "tool_calls": None,
                "_usage": {"input_tokens": 1, "output_tokens": 1},
                "_model": "test-model",
            },
        ]

        def fake_raw(config, messages, tools):
            return responses.pop(0)

        builtin_clients = {"local_shell": CancellingClient()}

        with patch("sys.stderr", io.StringIO()):
            reply, _usage = chat_turn(
                config={"chat_model": "test-model"},
                provider="openai",
                raw_fn=fake_raw,
                messages=[{"role": "user", "content": "do thing"}],
                tools=None,
                tool_map={},
                builtin_clients=builtin_clients,
                max_tool_rounds=3,
            )
        self.assertEqual(reply, "Okay, stopping.")


if __name__ == "__main__":
    unittest.main()
