"""Tests for explicit budgets, long-running tool turns and weak-model side tasks."""

import io
import json
import unittest
from unittest.mock import patch

from conch.providers import error_response
from conch.runtime import (
    auto_compact, chat_turn, estimate_tokens, side_task_fn, weak_model_config,
)


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
    def __init__(self):
        self.calls = 0

    def call_tool(self, name, arguments):
        self.calls += 1
        return {"content": [{"type": "text", "text": "ok"}]}


class TestGracefulExhaustion(unittest.TestCase):
    def test_explicit_round_budget_stops_tool_execution(self):
        calls = {"n": 0}
        client = _NoopClient()

        def raw_fn(config, messages, tools):
            calls["n"] += 1
            if tools is not None:
                return _tool_call_response(calls["n"])
            return {"content": "Progress: did A and B; remaining: C",
                    "tool_calls": None,
                    "_usage": {"input_tokens": 10, "output_tokens": 10},
                    "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            _, usage = chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "go"}],
                tools=[{"function": {"name": "noop"}}], tool_map={},
                builtin_clients={"noop": client}, max_tool_rounds=2,
            )
        self.assertEqual(client.calls, 2)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(usage["input_tokens"] + usage["output_tokens"], 320)

    def test_token_budget_stops_an_unlimited_turn(self):
        calls = {"n": 0}
        client = _NoopClient()

        def raw_fn(config, messages, tools):
            calls["n"] += 1
            if tools is not None:
                return _tool_call_response(calls["n"])  # 150 tokens/round
            return {"content": "stopped at budget", "tool_calls": None,
                    "_usage": {"input_tokens": 1, "output_tokens": 1},
                    "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            _, usage = chat_turn(
                config={"turn_token_budget": "200"}, provider="openai",
                raw_fn=raw_fn,
                messages=[{"role": "user", "content": "go"}],
                tools=[{"function": {"name": "noop"}}], tool_map={},
                builtin_clients={"noop": client},
            )
        self.assertEqual(client.calls, 2)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(usage["input_tokens"] + usage["output_tokens"], 302)


class TestToolContinuation(unittest.TestCase):
    def _run_long_turn(self, *, summary_fails):
        class ProgressClient:
            completed = 0

            def call_tool(self, name, arguments):
                step = arguments["step"]
                if step != self.completed + 1:
                    raise ValueError("Steps must execute once and in order")
                self.completed = step
                return {"content": [{
                    "type": "text",
                    "text": f"step {step}\n" + "detail " * 200,
                }]}

        client = ProgressClient()
        requests = 0
        tools = [{"type": "function", "function": {
            "name": "advance",
            "parameters": {"type": "object", "properties": {
                "step": {"type": "integer"},
            }, "required": ["step"]},
        }}]

        def raw_fn(config, messages, send_tools):
            nonlocal requests
            if send_tools is None:
                if summary_fails:
                    return error_response("Summary service unavailable")
                return {"content": "Complete 60 steps; progress is in the latest tool result."}
            requests += 1
            self.assertLessEqual(requests, 61, "The final answer must end the turn")
            self.assertLessEqual(estimate_tokens(messages, send_tools), 4000)
            pending = set()
            completed = 0
            for message in messages:
                if message.get("tool_calls"):
                    self.assertFalse(pending)
                    pending = {call["id"] for call in message["tool_calls"]}
                elif message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending)
                    pending.remove(message["tool_call_id"])
                    completed = int(message["content"].splitlines()[0].split()[1])
            self.assertFalse(pending)
            self.assertEqual(completed, client.completed)
            if completed == 60:
                return {"content": f"Finished {completed} steps."}
            return {"content": "", "tool_calls": [{
                "id": f"step-{completed + 1}", "type": "function",
                "function": {
                    "name": "advance",
                    "arguments": json.dumps({"step": completed + 1}),
                },
            }]}

        with patch("conch.runtime.get_context_limit", return_value=3600), \
             patch("conch.runtime._token_calibration", {}), \
             patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=[{"role": "system", "content": "Execute the requested steps."},
                          {"role": "user", "content": "Complete 60 steps in order."}],
                tools=tools, tool_map={}, builtin_clients={"advance": client},
            )
        self.assertEqual(client.completed, 60)

    def test_long_turn_compacts_and_preserves_tool_progress(self):
        self._run_long_turn(summary_fails=False)

    def test_long_turn_keeps_context_bounded_when_summaries_fail(self):
        self._run_long_turn(summary_fails=True)


class TestWeakModelConfig(unittest.TestCase):
    def test_none_when_unconfigured(self):
        self.assertIsNone(weak_model_config({}))
        self.assertIsNone(weak_model_config({"weak_model": "  "}))

    @patch(
        "conch.providers.validate_model_for_provider",
        return_value=(False, "not tool capable"),
    )
    def test_unverified_weak_model_is_rejected(self, _validate):
        self.assertIsNone(weak_model_config({
            "provider": "ollama",
            "weak_model": "plain-model",
        }))

    @patch(
        "conch.providers.validate_model_for_provider",
        return_value=(True, ""),
    )
    def test_same_provider_by_default(self, _validate):
        cfg = weak_model_config({
            "provider": "ollama", "chat_model": "qwen3.5:122b",
            "weak_model": "qwen3-8b",
        })
        self.assertEqual(cfg["provider"], "ollama")
        self.assertEqual(cfg["chat_model"], "qwen3-8b")
        self.assertEqual(cfg["model"], "qwen3-8b")

    @patch(
        "conch.providers.validate_model_for_provider",
        return_value=(True, ""),
    )
    def test_weak_provider_override(self, _validate):
        cfg = weak_model_config({
            "provider": "anthropic", "weak_model": "qwen3-8b",
            "weak_provider": "ollama",
        })
        self.assertEqual(cfg["provider"], "ollama")

    @patch(
        "conch.providers.validate_model_for_provider",
        return_value=(True, ""),
    )
    def test_parent_config_untouched(self, _validate):
        parent = {"provider": "ollama", "chat_model": "big", "weak_model": "small"}
        weak_model_config(parent)
        self.assertEqual(parent["chat_model"], "big")


class TestSideTaskFn(unittest.TestCase):
    def test_defaults_without_weak_model(self):
        sentinel_fn = object()
        fn, cfg = side_task_fn({"provider": "ollama"}, sentinel_fn, {"provider": "ollama"})
        self.assertIs(fn, sentinel_fn)

    @patch(
        "conch.providers.validate_model_for_provider",
        return_value=(True, ""),
    )
    def test_weak_model_selected(self, _validate):
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
             patch.dict("conch.providers.RAW_FNS", {"ollama": weak_raw_fn}), \
             patch("conch.providers.validate_model_for_provider",
                   return_value=(True, "")):
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
            {"role": "assistant", "content": "r"},
            {"role": "user", "content": "three"},
        ]
        config = {"provider": "ollama", "weak_model": "qwen3-8b"}
        with patch.dict("conch.providers.RAW_FNS", {"ollama": weak_raw_fn}), \
             patch("conch.providers.validate_model_for_provider",
                   return_value=(True, "")):
            _summarize_and_save(messages, config, main_raw_fn, memory)
        self.assertEqual(seen["model"], "qwen3-8b")
        self.assertEqual(len(memory.saved), 1)


if __name__ == "__main__":
    unittest.main()
