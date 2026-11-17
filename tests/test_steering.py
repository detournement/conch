"""Safe-boundary guidance through real AgentSession/runtime execution."""

import copy
import io
import unittest
from unittest.mock import patch

from conch.providers import error_response
from conch.render import StreamPrinter
from conch.session import AgentSession, SessionBudgets


def _call(name, call_id, value="original"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": {"value": value}},
    }


def _response(text="done", calls=None):
    return {
        "content": text,
        "tool_calls": calls,
        "_usage": {"input_tokens": 11, "output_tokens": 3},
        "_model": "steering-test",
    }


class RecordingClient:
    def __init__(self, after_call=None):
        self.calls = []
        self.after_call = after_call

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.after_call:
            self.after_call(name)
        return {"content": [{"type": "text", "text": f"finished {name}"}]}


class TestSafeBoundarySteering(unittest.TestCase):
    def setUp(self):
        self.pending = []
        self.requests = []
        self.history = [{"role": "user", "content": "perform the work"}]
        self.client = RecordingClient()
        self.config = {"provider": "openai", "chat_model": "steering-test"}
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                },
            }
            for name in ("first", "second")
        ]

    def take_steering(self):
        accepted = self.pending[:]
        self.pending.clear()
        return accepted

    def run_turn(self, respond, *, provider="openai", budgets=None, on_token=None):
        self.config["provider"] = provider
        session = AgentSession(self.config, budgets=budgets)
        session.attach_clients(
            {"first": self.client, "second": self.client}, bind=False
        )

        def raw_fn(config, messages, tools):
            self.requests.append(copy.deepcopy(messages))
            return respond(messages, tools)

        def stream_fn(config, messages, tools, callback):
            response = raw_fn(config, messages, tools)
            if response.get("content"):
                callback(response["content"])
            return response

        with patch.dict("conch.providers.RAW_FNS", {provider: raw_fn}), \
                patch.dict("conch.providers.STREAM_FNS", {provider: stream_fn}), \
                patch("sys.stderr", io.StringIO()):
            return session.run_turn(
                self.history,
                tools=self.tools,
                tool_map={},
                steering_fn=self.take_steering,
                on_token=on_token,
            )

    def assert_batch_order(self, messages, provider, expected_contents, guidance):
        if provider == "anthropic":
            index = next(
                i for i, message in enumerate(messages)
                if message["role"] == "assistant"
                and isinstance(message.get("content"), list)
                and any(b.get("type") == "tool_use" for b in message["content"])
            )
            calls = [
                block for block in messages[index]["content"]
                if block["type"] == "tool_use"
            ]
            result_message = messages[index + 1]
            self.assertEqual(result_message["role"], "user")
            results = result_message["content"]
            self.assertEqual([r["type"] for r in results], ["tool_result"] * len(calls))
            self.assertEqual([c["id"] for c in calls], [r["tool_use_id"] for r in results])
            following = index + 2
        else:
            index = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
            calls = messages[index]["tool_calls"]
            results = messages[index + 1:index + 1 + len(calls)]
            self.assertEqual([r["role"] for r in results], ["tool"] * len(calls))
            self.assertEqual([c["id"] for c in calls], [r["tool_call_id"] for r in results])
            following = index + 1 + len(calls)
        self.assertEqual(len(results), len(expected_contents))
        for result, expected in zip(results, expected_contents):
            self.assertIn(expected, result["content"])
        self.assertEqual(
            messages[following:following + len(guidance)],
            [{"role": "user", "content": text} for text in guidance],
        )

    def test_guidance_before_inference_informs_request_without_skipping_fresh_tools(self):
        self.pending.extend(["Use the first operation", "Keep the output short"])

        def respond(messages, tools):
            if len(self.requests) == 1:
                self.assertEqual(messages[-2:], [
                    {"role": "user", "content": "Use the first operation"},
                    {"role": "user", "content": "Keep the output short"},
                ])
                return _response(calls=[_call("first", "fresh")])
            return _response()

        reply, usage = self.run_turn(respond)
        self.assertEqual(reply, "done")
        self.assertEqual(self.client.calls, [("first", {"value": "original"})])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (22, 6))
        self.assertEqual(self.pending, [])

    def test_guidance_during_inference_skips_stale_batch_with_valid_provider_history(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                self.setUp()
                guidance = ["Do not execute that plan", "Explain the alternative instead"]

                def respond(messages, tools):
                    if len(self.requests) == 1:
                        self.pending.extend(guidance)
                        response = _response("Old plan", [
                            _call("first", "one"), _call("second", "two"),
                        ])
                        if provider == "anthropic":
                            response["_anthropic_content"] = [
                                {"type": "thinking", "thinking": "Old reasoning", "signature": "sig"},
                                {"type": "text", "text": "Old plan"},
                                {"type": "tool_use", "id": "one", "name": "first", "input": {"value": "original"}},
                                {"type": "tool_use", "id": "two", "name": "second", "input": {"value": "original"}},
                            ]
                        return response
                    self.assert_batch_order(messages, provider, ["Not executed:"] * 2, guidance)
                    return _response("Alternative")

                reply, usage = self.run_turn(respond, provider=provider)
                self.assertEqual(reply, "Alternative")
                self.assertEqual(self.client.calls, [])
                self.assertEqual(len(self.requests), 2)
                self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (22, 6))
                self.assertEqual(usage["model"], "steering-test")
                self.assert_batch_order(self.history, provider, ["Not executed:"] * 2, guidance)
                if provider == "anthropic":
                    self.assertEqual(self.history[1]["content"][0]["signature"], "sig")

    def test_running_tool_finishes_once_and_remaining_batch_is_superseded(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                self.setUp()
                guidance = ["Change the second operation"]

                def after_call(name):
                    if name == "first":
                        self.pending.extend(guidance)

                self.client.after_call = after_call

                def respond(messages, tools):
                    if len(self.requests) == 1:
                        return _response(calls=[_call("first", "one"), _call("second", "two")])
                    if len(self.requests) == 2:
                        self.assert_batch_order(messages, provider, ["finished first", "Not executed:"], guidance)
                        self.assertEqual(self.client.calls, [("first", {"value": "original"})])
                        return _response(calls=[_call("second", "revised", "changed")])
                    return _response("Updated work complete")

                reply, usage = self.run_turn(respond, provider=provider)
                self.assertEqual(reply, "Updated work complete")
                self.assertEqual(self.client.calls, [
                    ("first", {"value": "original"}),
                    ("second", {"value": "changed"}),
                ])
                self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (33, 9))

    def test_final_response_with_guidance_becomes_intermediate_context(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                self.setUp()
                tokens = []

                def respond(messages, tools):
                    if len(self.requests) == 1:
                        self.pending.append("Include the caveat")
                        return _response("Initial answer")
                    self.assertEqual(messages[-2:], [
                        {"role": "assistant", "content": "Initial answer"},
                        {"role": "user", "content": "Include the caveat"},
                    ])
                    return _response("Answer with caveat")

                reply, usage = self.run_turn(respond, provider=provider, on_token=tokens.append)
                self.assertEqual(reply, "Answer with caveat")
                self.assertEqual(tokens, ["Initial answer", "Answer with caveat"])
                self.assertEqual(self.history[-1]["content"], "Include the caveat")
                self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (22, 6))

    def test_unusable_final_response_with_guidance_stays_out_of_history(self):
        textual = '{"name": "first", "arguments": {"value": "x"}}'
        for provider in ("openai", "anthropic"):
            for content in ("", textual):
                with self.subTest(provider=provider, content=content):
                    self.setUp()

                    def respond(messages, tools):
                        if len(self.requests) == 1:
                            self.pending.append("Try a different approach")
                            return _response(content)
                        return _response("Recovered answer")

                    reply, usage = self.run_turn(respond, provider=provider)
                    self.assertEqual(reply, "Recovered answer")
                    self.assertEqual(
                        [(m["role"], m["content"]) for m in self.history],
                        [("user", "perform the work"),
                         ("user", "Try a different approach")],
                    )
                    self.assertEqual(bool(usage.get("tool_protocol_error")), bool(content))
                    self.assertEqual(self.client.calls, [])

    def test_blank_guidance_never_reaches_the_provider(self):
        self.pending.extend(["   ", "\n", "Real guidance"])
        reply, _ = self.run_turn(lambda messages, tools: _response("done"))
        self.assertEqual(reply, "done")
        self.assertEqual(
            [m["content"] for m in self.history],
            ["perform the work", "Real guidance"],
        )

    def test_tool_relevance_keeps_the_original_request_after_guidance(self):
        queries = []

        def relevant(tools, text, limit, pinned=None):
            queries.append(text)
            return tools[:limit]

        def respond(messages, tools):
            if len(self.requests) == 1:
                self.pending.append("shorter")
                return _response("Initial answer")
            return _response("Short answer")

        with patch.dict("conch.providers.PROVIDER_TOOL_LIMITS", {"openai": 1}), \
                patch("conch.tooling.select_relevant_tools", side_effect=relevant):
            self.run_turn(respond)
        self.assertEqual(len(queries), 2)
        self.assertIn("perform the work", queries[0])
        self.assertIn("perform the work", queries[1])
        self.assertIn("shorter", queries[1])

    def test_ui_stream_commits_intermediate_response_without_repeating_it(self):
        class Surface:
            def __init__(self):
                self.current = ""
                self.finished = []

            def update_response(self, text):
                self.current = text

            def finish_response(self, text):
                self.finished.append(text)
                self.current = ""

        surface = Surface()
        with patch("conch.render._get_active_ui", return_value=surface):
            printer = StreamPrinter(show_waiting=False)

        def respond(messages, tools):
            if len(self.requests) == 1:
                self.pending.append("Clarify")
                return _response("Initial answer")
            self.assertEqual(surface.current, "")
            self.assertEqual(len(surface.finished), 1)
            return _response("Clarified answer")

        reply, _ = self.run_turn(respond, on_token=printer.feed)
        self.assertEqual(reply, "Clarified answer")
        self.assertEqual(printer.flush(), "Clarified answer")
        self.assertEqual(len(surface.finished), 2)
        self.assertIn("Initial answer", surface.finished[0])
        self.assertIn("Clarified answer", surface.finished[1])
        self.assertNotIn("Initial answer", surface.finished[1])

    def test_steering_preserves_round_and_session_token_budgets(self):
        cases = [
            (SessionBudgets(max_tool_rounds=1), "tool round budget"),
            (SessionBudgets(max_tool_rounds=5, turn_token_budget=14), "token budget"),
        ]
        for budgets, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                self.config["turn_token_budget"] = 1000

                def respond(messages, tools):
                    if len(self.requests) == 1:
                        self.pending.append("Revise the answer")
                        return _response("Initial answer")
                    self.assertIsNone(tools)
                    self.assertIn(reason, messages[-1]["content"])
                    self.assertEqual(messages[-2]["content"], "Revise the answer")
                    # A completed turn must leave late guidance queued, not
                    # evade its budget with another inference round.
                    self.pending.append("Next turn guidance")
                    return _response("Budget summary")

                reply, usage = self.run_turn(respond, budgets=budgets)
                self.assertIn("Budget summary", reply)
                self.assertIn(reason + " reached", reply)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (22, 6))
                self.assertEqual(self.config["turn_token_budget"], 1000)
                self.assertEqual(self.pending, ["Next turn guidance"])

    def test_existing_retry_receives_guidance_before_request(self):
        def respond(messages, tools):
            if len(self.requests) == 1:
                self.pending.append("Narrow the answer")
                return error_response("connection timed out")
            self.assertEqual(messages[-1], {"role": "user", "content": "Narrow the answer"})
            return _response("Narrow answer")

        with patch("conch.runtime.time.sleep"):
            reply, _ = self.run_turn(respond)
        self.assertEqual(reply, "Narrow answer")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.history[-1]["content"], "Narrow the answer")


if __name__ == "__main__":
    unittest.main()
