"""Tests for the plan/todo scratchpad (plan 2.5) and delegate_task subagent
(plan 3.1)."""

import io
import json
import unittest
from unittest.mock import patch

from conch.tooling import (
    DELEGATE_TASK_TOOL,
    DelegateTaskClient,
    PINNED_TOOL_NAMES,
    TODO_LIST_TOOL,
    TodoListClient,
    ToolRuntimeState,
)
from conch.runtime import chat_turn


class TestTodoListClient(unittest.TestCase):
    def setUp(self):
        self.client = TodoListClient()

    def _call(self, args):
        return self.client.call_tool("todo_list", args)["content"][0]["text"]

    def test_add_and_render(self):
        self._call({"action": "add", "item": "read the file"})
        self._call({"action": "add", "items": ["run tests", "commit"]})
        rendered = self.client.render()
        self.assertIn("[Current plan", rendered)
        self.assertIn("#1 read the file", rendered)
        self.assertIn("#3 commit", rendered)

    def test_complete_marks_done(self):
        self._call({"action": "add", "item": "step one"})
        self._call({"action": "complete", "id": 1})
        self.assertIn("[x] #1 step one", self.client.render())

    def test_remove(self):
        self._call({"action": "add", "items": ["a", "b"]})
        self._call({"action": "remove", "id": 1})
        rendered = self.client.render()
        self.assertNotIn("#1 a", rendered)
        self.assertIn("#2 b", rendered)

    def test_clear_empties(self):
        self._call({"action": "add", "item": "x"})
        self._call({"action": "clear"})
        self.assertEqual(self.client.render(), "")

    def test_empty_renders_nothing(self):
        self.assertEqual(self.client.render(), "")

    def test_unknown_id_errors(self):
        self.assertIn("Error", self._call({"action": "complete", "id": 99}))

    def test_max_items_capped(self):
        self._call({"action": "add", "items": [f"i{n}" for n in range(50)]})
        self.assertLessEqual(len(self.client._items), TodoListClient.MAX_ITEMS)

    def test_pinned_for_local_models(self):
        self.assertIn("todo_list", PINNED_TOOL_NAMES)
        self.assertIn("delegate_task", PINNED_TOOL_NAMES)


class TestTodoInjection(unittest.TestCase):
    def test_state_reinjected_each_round(self):
        """The todo block must appear in the messages actually sent, without
        being appended to persistent history."""
        todo = TodoListClient()
        todo.call_tool("todo_list", {"action": "add", "item": "find the bug"})
        seen_payloads = []

        def raw_fn(config, send_messages, tools):
            seen_payloads.append([dict(m) for m in send_messages])
            return {"content": "ok", "tool_calls": None,
                    "_usage": {"input_tokens": 1, "output_tokens": 1},
                    "_model": "test"}

        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "go"}]
        with patch("sys.stderr", io.StringIO()):
            chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=messages, tools=None, tool_map={},
                builtin_clients={"todo_list": todo}, max_tool_rounds=2,
            )
        sent = seen_payloads[0]
        blocks = [m for m in sent if "Current plan" in str(m.get("content", ""))]
        self.assertEqual(len(blocks), 1, "todo state must be injected once")
        self.assertIn("find the bug", blocks[0]["content"])
        # persistent history untouched
        self.assertEqual(len(messages), 2)

    def test_empty_todo_injects_nothing(self):
        todo = TodoListClient()
        seen = []

        def raw_fn(config, send_messages, tools):
            seen.append(list(send_messages))
            return {"content": "ok", "tool_calls": None,
                    "_usage": {}, "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=[{"role": "user", "content": "hi"}],
                tools=None, tool_map={},
                builtin_clients={"todo_list": todo}, max_tool_rounds=2,
            )
        self.assertEqual(len(seen[0]), 1)

    def test_anthropic_gets_todo_in_system_string(self):
        todo = TodoListClient()
        todo.call_tool("todo_list", {"action": "add", "item": "plan step"})
        seen = []

        def raw_fn(config, send_messages, tools):
            seen.append([dict(m) for m in send_messages])
            return {"content": "ok", "tool_calls": None,
                    "_usage": {}, "_model": "test"}

        with patch("sys.stderr", io.StringIO()):
            chat_turn(
                config={}, provider="anthropic", raw_fn=raw_fn,
                messages=[{"role": "system", "content": "sys"},
                          {"role": "user", "content": "hi"}],
                tools=None, tool_map={},
                builtin_clients={"todo_list": todo}, max_tool_rounds=2,
            )
        system_msgs = [m for m in seen[0] if m["role"] == "system"]
        self.assertEqual(len(system_msgs), 1,
                         "anthropic must get exactly one system message")
        self.assertIn("plan step", system_msgs[0]["content"])


class _FakeShellClient:
    name = "local_shell"

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": "fake output"}]}


def _make_delegate(config=None, tools=None):
    client = DelegateTaskClient()
    state = ToolRuntimeState(
        all_tools=tools or [], tool_map={}, tools=tools or []
    )
    builtins = {"local_shell": _FakeShellClient(), "delegate_task": client,
                "conch_config": object(), "todo_list": TodoListClient()}
    client.bind(config if config is not None else {"provider": "openai"}, state, builtins)
    return client


class TestDelegateTask(unittest.TestCase):
    def test_runs_fresh_chat_turn_and_returns_summary(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, max_tool_rounds=25, **kw):
            seen["messages"] = messages
            seen["tools"] = tools
            seen["clients"] = builtin_clients
            seen["rounds"] = max_tool_rounds
            return "did the thing; found X", {"input_tokens": 10, "output_tokens": 5, "model": "m"}

        client = _make_delegate(tools=[
            {"function": {"name": "local_shell"}},
            {"function": {"name": "delegate_task"}},
            {"function": {"name": "conch_config"}},
        ])
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            result = client.call_tool("delegate_task", {"task": "inspect the repo"})
        text = result["content"][0]["text"]
        self.assertIn("did the thing; found X", text)
        # fresh clean context: system + task only
        self.assertEqual(len(seen["messages"]), 2)
        self.assertEqual(seen["messages"][1]["content"], "inspect the repo")
        # narrowed toolset: no recursion, no self-management
        tool_names = {t["function"]["name"] for t in seen["tools"]}
        self.assertEqual(tool_names, {"local_shell"})
        self.assertNotIn("delegate_task", seen["clients"])
        self.assertNotIn("conch_config", seen["clients"])
        # own round budget
        self.assertEqual(seen["rounds"], DelegateTaskClient.DEFAULT_ROUNDS)

    def test_context_appended_to_task(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, *a, **kw):
            seen["messages"] = messages
            return "ok", {}

        client = _make_delegate()
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t", "context": "extra info"})
        self.assertIn("extra info", seen["messages"][1]["content"])

    def test_subagent_rounds_configurable(self):
        seen = {}

        def fake_chat_turn(*args, max_tool_rounds=25, **kw):
            seen["rounds"] = max_tool_rounds
            return "ok", {}

        client = _make_delegate(config={"provider": "openai", "subagent_rounds": "4"})
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t"})
        self.assertEqual(seen["rounds"], 4)

    def test_subagent_model_applied(self):
        seen = {}

        def fake_chat_turn(config, *args, **kw):
            seen["config"] = config
            return "ok", {}

        client = _make_delegate(config={
            "provider": "openai", "chat_model": "gpt-4o", "model": "gpt-4o",
            "subagent_model": "gpt-4o-mini",
        })
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t"})
        self.assertEqual(seen["config"]["chat_model"], "gpt-4o-mini")

    def test_ollama_subagent_model_validated(self):
        seen = {}

        def fake_chat_turn(config, *args, **kw):
            seen["config"] = config
            return "ok", {}

        client = _make_delegate(config={
            "provider": "ollama", "chat_model": "qwen3.6:27b", "model": "qwen3.6:27b",
            "subagent_model": "not-installed",
        })
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("conch.providers.validate_ollama_model",
                   return_value=(False, "model 'not-installed' is not installed on the Ollama server")), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t"})
        self.assertEqual(seen["config"]["chat_model"], "qwen3.6:27b",
                         "invalid subagent model must fall back to the parent's")

    def test_serialized_execution(self):
        client = _make_delegate()

        def nested_chat_turn(*args, **kw):
            # Re-entrant call while the first is still running
            inner = client.call_tool("delegate_task", {"task": "nested"})
            return inner["content"][0]["text"], {}

        with patch("conch.runtime.chat_turn", nested_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            result = client.call_tool("delegate_task", {"task": "outer"})
        self.assertIn("one at a time", result["content"][0]["text"])

    def test_missing_task_errors(self):
        client = _make_delegate()
        result = client.call_tool("delegate_task", {})
        self.assertIn("Error", result["content"][0]["text"])

    def test_parent_config_not_mutated(self):
        parent = {"provider": "openai", "chat_model": "gpt-4o", "model": "gpt-4o",
                  "subagent_model": "gpt-4o-mini"}

        def fake_chat_turn(config, *args, **kw):
            config["provider"] = "mutated"  # simulates fallback switching
            return "ok", {}

        client = _make_delegate(config=parent)
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "t"})
        self.assertEqual(parent["provider"], "openai")

    def test_tool_definitions_registered(self):
        self.assertEqual(TODO_LIST_TOOL["function"]["name"], "todo_list")
        self.assertEqual(DELEGATE_TASK_TOOL["function"]["name"], "delegate_task")


if __name__ == "__main__":
    unittest.main()
