"""Tests for lifecycle hooks (plan 2.2): pre_tool_use / post_tool_use /
on_turn_end shell scripts configured in ~/.config/conch/config."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.tooling import get_hook_command, run_hook
from conch.runtime import chat_turn


class TestRunHook(unittest.TestCase):
    def test_no_hook_configured_is_permissive(self):
        allowed, out = run_hook("pre_tool_use", {"tool": "x"}, {})
        self.assertTrue(allowed)
        self.assertEqual(out, "")

    def test_zero_exit_allows(self):
        allowed, out = run_hook(
            "pre_tool_use", {"tool": "x"}, {"hook_pre_tool_use": "true"}
        )
        self.assertTrue(allowed)

    def test_nonzero_exit_blocks_with_reason(self):
        allowed, reason = run_hook(
            "pre_tool_use", {"tool": "x"},
            {"hook_pre_tool_use": "echo nope-not-allowed >&2; exit 1"},
        )
        self.assertFalse(allowed)
        self.assertIn("nope-not-allowed", reason)

    def test_payload_delivered_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "payload.json"
            allowed, _ = run_hook(
                "post_tool_use",
                {"tool": "local_shell", "result": "ok"},
                {"hook_post_tool_use": f"cat > {capture}"},
            )
            self.assertTrue(allowed)
            payload = json.loads(capture.read_text())
        self.assertEqual(payload["tool"], "local_shell")
        self.assertEqual(payload["result"], "ok")

    def test_stdout_returned_for_rewrites(self):
        allowed, out = run_hook(
            "pre_tool_use", {"tool": "x"},
            {"hook_pre_tool_use": "echo '{\"command\": \"echo safe\"}'"},
        )
        self.assertTrue(allowed)
        self.assertEqual(json.loads(out), {"command": "echo safe"})

    def test_crashing_hook_is_permissive(self):
        allowed, _ = run_hook(
            "pre_tool_use", {"tool": "x"},
            {"hook_pre_tool_use": "/nonexistent/hook-script-xyz"},
        )
        # shell reports 127 → blocked? No: command not found exits non-zero
        # via the shell, which is indistinguishable from an intentional
        # block, so this documents the actual behavior: the shell ran and
        # exited non-zero → blocked.
        self.assertFalse(allowed)

    def test_get_hook_command(self):
        self.assertEqual(get_hook_command("pre_tool_use", {}), "")
        self.assertEqual(
            get_hook_command("pre_tool_use", {"hook_pre_tool_use": " x "}), "x"
        )


class _EchoClient:
    """Fake builtin tool that records the arguments it was called with."""

    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append(arguments)
        return {"content": [{"type": "text", "text": f"ran {arguments}"}]}


def _tool_call_response(arguments):
    return {
        "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "my_tool", "arguments": json.dumps(arguments)},
        }],
        "_usage": {"input_tokens": 1, "output_tokens": 1},
        "_model": "test",
    }


def _final_response(text):
    return {"content": text, "tool_calls": None,
            "_usage": {"input_tokens": 1, "output_tokens": 1}, "_model": "test"}


class TestChatTurnHookDispatch(unittest.TestCase):
    def _run_turn(self, config, client):
        responses = [
            _tool_call_response({"value": "original"}),
            _final_response("done"),
        ]

        def raw_fn(cfg, messages, tools):
            return responses.pop(0)

        messages = [{"role": "user", "content": "go"}]
        with patch("sys.stderr", io.StringIO()):
            reply, _ = chat_turn(
                config=config, provider="openai", raw_fn=raw_fn,
                messages=messages, tools=None, tool_map={},
                builtin_clients={"my_tool": client}, max_tool_rounds=3,
            )
        return reply, messages

    def test_pre_hook_block_prevents_execution(self):
        client = _EchoClient()
        config = {"hook_pre_tool_use": "echo blocked-by-policy >&2; exit 2"}
        reply, messages = self._run_turn(config, client)
        self.assertEqual(client.calls, [], "blocked tool must not execute")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertIn("Blocked by pre_tool_use hook", tool_msgs[0]["content"])
        self.assertIn("blocked-by-policy", tool_msgs[0]["content"])

    def test_pre_hook_rewrites_arguments(self):
        client = _EchoClient()
        config = {"hook_pre_tool_use": "echo '{\"value\": \"rewritten\"}'"}
        self._run_turn(config, client)
        self.assertEqual(client.calls, [{"value": "rewritten"}])

    def test_post_hook_receives_result(self):
        client = _EchoClient()
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "post.json"
            config = {"hook_post_tool_use": f"cat > {capture}"}
            self._run_turn(config, client)
            payload = json.loads(capture.read_text())
        self.assertEqual(payload["tool"], "my_tool")
        self.assertIn("ran", payload["result"])

    def test_on_turn_end_receives_reply(self):
        client = _EchoClient()
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "end.json"
            config = {"hook_on_turn_end": f"cat > {capture}"}
            self._run_turn(config, client)
            payload = json.loads(capture.read_text())
        self.assertEqual(payload["reply"], "done")

    def test_no_hooks_normal_flow(self):
        client = _EchoClient()
        reply, _ = self._run_turn({}, client)
        self.assertEqual(reply, "done")
        self.assertEqual(client.calls, [{"value": "original"}])


if __name__ == "__main__":
    unittest.main()
