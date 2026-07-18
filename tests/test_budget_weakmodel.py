"""Tests for graceful budget exhaustion (plan 2.6) and weak-model side tasks
(plan 2.7)."""

import io
import json
import unittest
from unittest.mock import patch

from conch.providers import error_response
from conch.runtime import auto_compact, chat_turn, side_task_fn, weak_model_config


def _tool_call_response(n):
    return {
        "content": "",
        "tool_calls": [{
            "id": f"c{n}", "type": "function",
            "function": {"name": "noop", "arguments": "{}"},
        }],
        "_usage": {"input_tokens": 100, "output_tokens": 50},
        "_model": "test",
    }


class _NoopClient:
    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": "ok"}]}


class TestGracefulExhaustion(unittest.TestCase):
    def test_round_budget_summarizes_progress(self):
        calls = {"n": 0}

        def raw_fn(config, messages, tools):
            calls["n"] += 1
            if tools is not None:
                return _tool_call_response(calls["n"])
            # The exhaustion summary call has no tools
            self.assertIn("Summarize", messages[-1]["content"])
            return {"content": "Progress: did A and B; remaining: C",
                    "tool_calls": None,
                    "_usage": {"input_tokens": 10, "output_tokens": 10},
                    "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            reply, usage = chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "go"}],
                tools=[{"function": {"name": "noop"}}], tool_map={},
                builtin_clients={"noop": _NoopClient()}, max_tool_rounds=2,
            )
        self.assertIn("Progress: did A and B", reply)
        self.assertIn("tool round budget reached", reply)
        self.assertNotIn("[max tool call rounds reached]", reply)

    def test_summary_failure_falls_back_to_marker(self):
        def raw_fn(config, messages, tools):
            if tools is not None:
                return _tool_call_response(0)
            return error_response("connection refused")

        with patch("sys.stderr", io.StringIO()):
            reply, _ = chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "go"}],
                tools=[{"function": {"name": "noop"}}], tool_map={},
                builtin_clients={"noop": _NoopClient()}, max_tool_rounds=1,
            )
        self.assertEqual(reply, "[tool round budget reached]")

    def test_token_budget_stops_early(self):
        calls = {"n": 0}

        def raw_fn(config, messages, tools):
            calls["n"] += 1
            if tools is not None:
                return _tool_call_response(calls["n"])  # 150 tokens/round
            return {"content": "stopped at budget", "tool_calls": None,
                    "_usage": {"input_tokens": 1, "output_tokens": 1},
                    "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            reply, usage = chat_turn(
                config={"turn_token_budget": "200"}, provider="openai",
                raw_fn=raw_fn,
                messages=[{"role": "user", "content": "go"}],
                tools=[{"function": {"name": "noop"}}], tool_map={},
                builtin_clients={"noop": _NoopClient()}, max_tool_rounds=50,
            )
        self.assertIn("stopped at budget", reply)
        self.assertIn("token budget reached", reply)
        # 2 tool rounds (300 tokens) trip the 200-token budget on round 3
        self.assertLessEqual(calls["n"], 3 + 1)

    def test_no_budget_uses_all_rounds(self):
        calls = {"n": 0}

        def raw_fn(config, messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_response(1)
            return {"content": "done", "tool_calls": None,
                    "_usage": {"input_tokens": 1, "output_tokens": 1},
                    "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            reply, _ = chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "go"}],
                tools=[{"function": {"name": "noop"}}], tool_map={},
                builtin_clients={"noop": _NoopClient()}, max_tool_rounds=5,
            )
        self.assertEqual(reply, "done")


class TestWeakModelConfig(unittest.TestCase):
    def test_none_when_unconfigured(self):
        self.assertIsNone(weak_model_config({}))
        self.assertIsNone(weak_model_config({"weak_model": "  "}))

    def test_same_provider_by_default(self):
        cfg = weak_model_config({
            "provider": "ollama", "chat_model": "qwen3.5:122b",
            "weak_model": "qwen3-8b",
        })
        self.assertEqual(cfg["provider"], "ollama")
        self.assertEqual(cfg["chat_model"], "qwen3-8b")
        self.assertEqual(cfg["model"], "qwen3-8b")

    def test_weak_provider_override(self):
        cfg = weak_model_config({
            "provider": "anthropic", "weak_model": "qwen3-8b",
            "weak_provider": "ollama",
        })
        self.assertEqual(cfg["provider"], "ollama")

    def test_parent_config_untouched(self):
        parent = {"provider": "ollama", "chat_model": "big", "weak_model": "small"}
        weak_model_config(parent)
        self.assertEqual(parent["chat_model"], "big")


class TestSideTaskFn(unittest.TestCase):
    def test_defaults_without_weak_model(self):
        sentinel_fn = object()
        fn, cfg = side_task_fn({"provider": "ollama"}, sentinel_fn, {"provider": "ollama"})
        self.assertIs(fn, sentinel_fn)

    def test_weak_model_selected(self):
        from conch.providers import RAW_FNS
        fn, cfg = side_task_fn(
            {"provider": "ollama", "weak_model": "qwen3-8b"}, object(), {}
        )
        self.assertIs(fn, RAW_FNS["ollama"])
        self.assertEqual(cfg["chat_model"], "qwen3-8b")


class TestWeakModelCompaction(unittest.TestCase):
    def test_auto_compact_uses_weak_model(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(10):
            msgs.append({"role": "user", "content": f"q{i} " + "x" * 200})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 200})
        seen = {}

        def weak_raw_fn(config, summary_messages, tools):
            seen["model"] = config.get("chat_model")
            return {"content": "- summary", "tool_calls": None}

        def main_raw_fn(config, summary_messages, tools):
            raise AssertionError("main model must not run the compaction")

        config = {"provider": "ollama", "chat_model": "qwen3.5:122b",
                  "weak_model": "qwen3-8b"}
        with patch("conch.runtime.get_context_limit", return_value=100), \
             patch.dict("conch.providers.RAW_FNS", {"ollama": weak_raw_fn}):
            changed = auto_compact(msgs, None, "ollama", config, main_raw_fn)
        self.assertTrue(changed)
        self.assertEqual(seen["model"], "qwen3-8b")

    def test_session_summary_uses_weak_model(self):
        from conch.app import _summarize_and_save

        class FakeMemory:
            def __init__(self):
                self.saved = []

            def add(self, content, source=""):
                self.saved.append(content)

        seen = {}

        def weak_raw_fn(config, messages, tools):
            seen["model"] = config.get("chat_model")
            return {"content": "- talked about X", "tool_calls": None}

        def main_raw_fn(config, messages, tools):
            raise AssertionError("main model must not run the summary")

        memory = FakeMemory()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "r"},
            {"role": "user", "content": "two"},
        ]
        config = {"provider": "ollama", "weak_model": "qwen3-8b"}
        with patch.dict("conch.providers.RAW_FNS", {"ollama": weak_raw_fn}):
            _summarize_and_save(messages, config, main_raw_fn, memory)
        self.assertEqual(seen["model"], "qwen3-8b")
        self.assertEqual(len(memory.saved), 1)


if __name__ == "__main__":
    unittest.main()
