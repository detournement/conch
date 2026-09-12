"""Required policy layer (Swarm Phase 0): fail-closed, distinct from hooks.

The invariant proven here: user hooks (tooling.run_hook) stay fail-open on
infrastructure failure, while required policy checks deny on exception,
timeout, or invalid decisions — and the denial gates real tool dispatch.
"""

import io
import json
import time
import unittest
from unittest.mock import patch

from conch.policy import (
    REQUIRED_POLICY,
    PolicyDecision,
    PolicyRegistry,
    evaluate_required_policy,
    register_required_policy,
    unregister_required_policy,
)
from conch.runtime import chat_turn
from conch.tooling import run_hook


class TestPolicyDecision(unittest.TestCase):
    def test_allow_and_deny_constructors(self):
        self.assertTrue(PolicyDecision.allow().allowed)
        denial = PolicyDecision.deny("nope", check="budget")
        self.assertFalse(denial.allowed)
        self.assertEqual(denial.reason, "nope")
        self.assertEqual(denial.check, "budget")


class TestPolicyRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = PolicyRegistry(timeout=0.5)

    def test_empty_registry_allows(self):
        decision = self.registry.evaluate("pre_tool_use", {"tool": "x"})
        self.assertTrue(decision.allowed)
        self.assertIn("no required policy checks", decision.reason)

    def test_all_allowing_checks_allow(self):
        self.registry.register("a", lambda event, payload: True)
        self.registry.register(
            "b", lambda event, payload: PolicyDecision.allow("fine")
        )
        decision = self.registry.evaluate("pre_tool_use", {})
        self.assertTrue(decision.allowed)

    def test_false_return_denies_with_check_name(self):
        self.registry.register("veto", lambda event, payload: False)
        decision = self.registry.evaluate("pre_tool_use", {})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.check, "veto")

    def test_deny_decision_carries_reason(self):
        self.registry.register(
            "budget",
            lambda event, payload: PolicyDecision.deny("over budget"),
        )
        decision = self.registry.evaluate("pre_tool_use", {})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "over budget")
        self.assertEqual(decision.check, "budget")

    def test_exception_denies(self):
        def broken(event, payload):
            raise RuntimeError("boom")

        self.registry.register("broken", broken)
        decision = self.registry.evaluate("pre_tool_use", {})
        self.assertFalse(decision.allowed)
        self.assertIn("RuntimeError", decision.reason)
        self.assertIn("failing closed", decision.reason)

    def test_timeout_denies(self):
        registry = PolicyRegistry(timeout=0.1)

        def hang(event, payload):
            time.sleep(2)
            return True

        registry.register("hang", hang)
        started = time.time()
        decision = registry.evaluate("pre_tool_use", {})
        self.assertFalse(decision.allowed)
        self.assertIn("timed out", decision.reason)
        self.assertLess(time.time() - started, 1.5)

    def test_invalid_decision_denies(self):
        for bad in (None, "yes", 1, {"allowed": True}):
            registry = PolicyRegistry(timeout=0.5)
            registry.register("weird", lambda event, payload, b=bad: b)
            with self.subTest(bad=bad):
                decision = registry.evaluate("pre_tool_use", {})
                self.assertFalse(decision.allowed)
                self.assertIn("invalid decision", decision.reason)

    def test_first_deny_short_circuits(self):
        calls = []

        def deny(event, payload):
            calls.append("deny")
            return False

        def never(event, payload):
            calls.append("never")
            return True

        self.registry.register("deny", deny)
        self.registry.register("never", never)
        decision = self.registry.evaluate("pre_tool_use", {})
        self.assertFalse(decision.allowed)
        self.assertEqual(calls, ["deny"])

    def test_checks_receive_event_and_payload(self):
        seen = []

        def check(event, payload):
            seen.append((event, dict(payload)))
            return True

        self.registry.register("spy", check)
        self.registry.evaluate("pre_tool_use", {"tool": "local_shell"})
        self.assertEqual(
            seen, [("pre_tool_use", {"tool": "local_shell"})]
        )

    def test_registration_validation(self):
        with self.assertRaises(ValueError):
            self.registry.register("", lambda e, p: True)
        with self.assertRaises(ValueError):
            self.registry.register("x", "not-callable")
        self.registry.register("x", lambda e, p: True)
        with self.assertRaises(ValueError):
            self.registry.register("x", lambda e, p: True)
        self.assertTrue(self.registry.unregister("x"))
        self.assertFalse(self.registry.unregister("x"))
        self.assertEqual(self.registry.names(), [])


class TestFailOpenVersusFailClosed(unittest.TestCase):
    """The layer-defining contrast, side by side."""

    def test_user_hook_timeout_is_permissive(self):
        with patch("sys.stderr", io.StringIO()):
            allowed, out = run_hook(
                "pre_tool_use", {"tool": "x"},
                {"hook_pre_tool_use": "sleep 5"}, timeout=0.2,
            )
        self.assertTrue(allowed, "user hooks stay fail-open on timeout")

    def test_required_policy_timeout_denies(self):
        registry = PolicyRegistry(timeout=0.2)
        registry.register("hang", lambda e, p: time.sleep(5) or True)
        decision = registry.evaluate("pre_tool_use", {"tool": "x"})
        self.assertFalse(
            decision.allowed, "required policy fails closed on timeout"
        )


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append(arguments)
        return {"content": [{"type": "text", "text": "tool ran"}]}


def _tool_call_response():
    return {
        "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {
                "name": "my_tool",
                "arguments": json.dumps({"value": "v"}),
            },
        }],
        "_usage": {"input_tokens": 1, "output_tokens": 1},
        "_model": "test",
    }


def _final_response(text):
    return {"content": text, "tool_calls": None,
            "_usage": {"input_tokens": 1, "output_tokens": 1},
            "_model": "test"}


class TestRequiredPolicyGatesToolDispatch(unittest.TestCase):
    """A registered denying check must stop real tool execution in
    chat_turn while user hooks remain untouched."""

    def setUp(self):
        REQUIRED_POLICY.clear()
        self.addCleanup(REQUIRED_POLICY.clear)

    def _run_turn(self, client):
        responses = [_tool_call_response(), _final_response("done")]

        def raw_fn(cfg, messages, tools):
            return responses.pop(0)

        messages = [{"role": "user", "content": "go"}]
        tools = [{
            "type": "function",
            "function": {
                "name": "my_tool",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        }]
        with patch("sys.stderr", io.StringIO()):
            reply, _ = chat_turn(
                config={}, provider="openai", raw_fn=raw_fn,
                messages=messages, tools=tools, tool_map={},
                builtin_clients={"my_tool": client}, max_tool_rounds=3,
            )
        return reply, messages

    def test_denying_check_blocks_execution(self):
        register_required_policy(
            "no-my-tool",
            lambda event, payload: PolicyDecision.deny(
                "my_tool is not authorized"
            ) if payload.get("tool") == "my_tool" else True,
        )
        client = _RecordingClient()
        reply, messages = self._run_turn(client)
        self.assertEqual(client.calls, [], "denied tool must not execute")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertIn("Denied by required policy", tool_msgs[0]["content"])
        self.assertIn("my_tool is not authorized", tool_msgs[0]["content"])

    def test_crashing_check_blocks_execution(self):
        def broken(event, payload):
            raise ValueError("policy bug")

        register_required_policy("broken", broken)
        client = _RecordingClient()
        reply, messages = self._run_turn(client)
        self.assertEqual(client.calls, [],
                         "a crashing required check must fail closed")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertIn("Denied by required policy", tool_msgs[0]["content"])

    def test_empty_registry_leaves_dispatch_unchanged(self):
        client = _RecordingClient()
        reply, _ = self._run_turn(client)
        self.assertEqual(reply, "done")
        self.assertEqual(client.calls, [{"value": "v"}])

    def test_module_helpers_operate_on_default_registry(self):
        register_required_policy("x", lambda e, p: False)
        decision = evaluate_required_policy("pre_tool_use", {})
        self.assertFalse(decision.allowed)
        self.assertTrue(unregister_required_policy("x"))
        self.assertTrue(evaluate_required_policy("pre_tool_use", {}).allowed)


if __name__ == "__main__":
    unittest.main()
