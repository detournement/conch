import io
import json
import unittest
from unittest.mock import patch

from conch.providers import error_response
from conch.runtime import chat_turn, compress_context


def _tool(name, *, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


def _call(name, arguments, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class RecordingClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}


class TestExactRequestAuthorization(unittest.TestCase):
    def _run(self, responses, tools, clients, messages=None, **config):
        iterator = iter(responses)
        history = messages or [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "do it"},
        ]
        with patch("sys.stderr", io.StringIO()):
            reply, usage = chat_turn(
                {"provider": "custom", **config},
                "custom",
                lambda *_: next(iterator),
                history,
                tools,
                {},
                clients,
                max_tool_rounds=6,
            )
        return reply, usage, history

    def test_unoffered_tool_never_executes_even_if_client_exists(self):
        hidden = RecordingClient()
        responses = [
            {
                "content": "",
                "tool_calls": [_call("hidden", "{}")],
                "_usage": {},
            },
            {"content": "done", "tool_calls": None, "_usage": {}},
        ]
        _, _, history = self._run(
            responses, [_tool("visible")], {"hidden": hidden}
        )
        self.assertEqual(hidden.calls, [])
        self.assertIn("was not offered", history[-1]["content"])

    def test_retry_reuses_exact_selected_tool_set(self):
        seen = []

        def raw_fn(_config, _messages, tools):
            seen.append([tool["function"]["name"] for tool in tools])
            if len(seen) == 1:
                return error_response("connection timed out")
            return {"content": "ok", "tool_calls": None, "_usage": {}}

        tools = [_tool(f"tool_{index}") for index in range(20)]
        with patch("sys.stderr", io.StringIO()), patch(
            "conch.runtime.time.sleep"
        ):
            chat_turn(
                {"provider": "custom"},
                "custom",
                raw_fn,
                [{"role": "user", "content": "use tool_19"}],
                tools,
                {},
                {},
                max_tool_rounds=2,
            )
        self.assertEqual(seen[0], seen[1])
        self.assertLessEqual(len(seen[0]), 12)


class TestArgumentAndIdIntegrity(TestExactRequestAuthorization):
    def test_malformed_arguments_are_rejected_not_coerced(self):
        client = RecordingClient()
        responses = [
            {
                "content": "",
                "tool_calls": [_call("safe", "{not-json")],
                "_usage": {},
            },
            {"content": "fixed", "tool_calls": None, "_usage": {}},
        ]
        _, _, history = self._run(
            responses, [_tool("safe")], {"safe": client}
        )
        self.assertEqual(client.calls, [])
        self.assertIn("not valid JSON", history[-1]["content"])

    def test_schema_required_fields_are_enforced(self):
        client = RecordingClient()
        responses = [
            {
                "content": "",
                "tool_calls": [_call("safe", "{}")],
                "_usage": {},
            },
            {"content": "fixed", "tool_calls": None, "_usage": {}},
        ]
        self._run(
            responses,
            [_tool("safe", required=("value",))],
            {"safe": client},
        )
        self.assertEqual(client.calls, [])

    def test_duplicate_json_keys_are_rejected(self):
        client = RecordingClient()
        responses = [
            {
                "content": "",
                "tool_calls": [
                    _call(
                        "safe",
                        '{"value":"first","value":"second"}',
                    )
                ],
                "_usage": {},
            },
            {"content": "fixed", "tool_calls": None, "_usage": {}},
        ]
        self._run(
            responses, [_tool("safe")], {"safe": client}
        )
        self.assertEqual(client.calls, [])

    def test_parallel_call_limit_rejects_entire_batch(self):
        client = RecordingClient()
        responses = [
            {
                "content": "",
                "tool_calls": [
                    _call("safe", "{}", f"call-{index}")
                    for index in range(3)
                ],
                "_usage": {},
            },
            {"content": "fixed", "tool_calls": None, "_usage": {}},
        ]
        _, usage, history = self._run(
            responses,
            [_tool("safe")],
            {"safe": client},
            max_parallel_tool_calls=2,
        )
        self.assertEqual(client.calls, [])
        self.assertIn("parallel tool calls", usage["tool_protocol_error"])
        self.assertEqual(
            len([m for m in history if m.get("role") == "tool"]), 2
        )

    def test_missing_and_duplicate_ids_are_replaced_before_results(self):
        client = RecordingClient()
        history = [
            {"role": "assistant", "content": "", "tool_calls": [
                _call("safe", "{}", call_id="duplicate")
            ]},
            {
                "role": "tool",
                "tool_call_id": "duplicate",
                "content": "old",
            },
            {"role": "user", "content": "again"},
        ]
        responses = [
            {
                "content": "",
                "tool_calls": [
                    _call("safe", "{}", call_id="duplicate"),
                    {
                        "type": "function",
                        "function": {
                            "name": "safe",
                            "arguments": "{}",
                        },
                    },
                ],
                "_usage": {},
            },
            {"content": "done", "tool_calls": None, "_usage": {}},
        ]
        self._run(
            responses,
            [_tool("safe")],
            {"safe": client},
            messages=history,
        )
        assistant = history[-3]
        results = history[-2:]
        ids = [call["id"] for call in assistant["tool_calls"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn("duplicate", ids)
        self.assertEqual(ids, [result["tool_call_id"] for result in results])


class TestCompactionProtocolGroups(unittest.TestCase):
    def test_openai_tool_group_is_never_split(self):
        messages = [{"role": "system", "content": "s"}]
        for index in range(10):
            messages.extend([
                {"role": "user", "content": "x" * 2000},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_call("t", "{}", f"c-{index}")],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"c-{index}",
                    "content": "y" * 2000,
                },
            ])
        with patch("conch.runtime.get_context_limit", return_value=1000):
            compacted = compress_context(messages, None, "custom", {})
        for index, message in enumerate(compacted):
            if message.get("role") == "tool":
                self.assertGreater(index, 0)
                self.assertEqual(
                    compacted[index - 1].get("role"), "assistant"
                )

    def test_tool_results_share_one_round_budget(self):
        clients = {
            f"tool_{index}": RecordingClient() for index in range(4)
        }
        calls = [
            _call(f"tool_{index}", "{}", f"call-{index}")
            for index in range(4)
        ]
        for client in clients.values():
            client.call_tool = lambda *_: {
                "content": [{"type": "text", "text": "z" * 10000}]
            }
        responses = iter([
            {"content": "", "tool_calls": calls, "_usage": {}},
            {"content": "done", "tool_calls": None, "_usage": {}},
        ])
        history = [{"role": "user", "content": "run"}]
        with patch("sys.stderr", io.StringIO()), patch(
            "conch.runtime.get_context_limit", return_value=1000
        ):
            chat_turn(
                {"provider": "custom"},
                "custom",
                lambda *_: next(responses),
                history,
                [_tool(name) for name in clients],
                {},
                clients,
            )
        result_chars = sum(
            len(message["content"])
            for message in history
            if message.get("role") == "tool"
        )
        self.assertLessEqual(result_chars, 350)


if __name__ == "__main__":
    unittest.main()
