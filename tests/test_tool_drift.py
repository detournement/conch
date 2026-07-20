"""Tests for tool-call drift control: few-shot anchor, drift detection, the
corrective reminder, and the /resettools reset.

Mechanism being defended against: small local models sometimes fall out of
native function-calling and emit tool calls as *text*. Well-formed textual
calls are recovered and re-stored as structured tool_calls, but malformed /
unregistered ones become the assistant reply and are persisted as prose —
which the model then imitates on the next turn, compounding the drift. The
defenses are a tiny always-on few-shot exemplar (local providers only), a
drift counter that escalates to a corrective reminder, and a manual reset.

All tests here are offline: pure functions plus chat_turn driven by a fake
provider. Nothing contacts a live Ollama server.
"""

import io
import json
import unittest
from unittest.mock import patch

from conch.runtime import (
    DRIFT_REMINDER_THRESHOLD,
    TOOL_CALL_REMINDER,
    apply_tool_call_scaffolding,
    build_tool_call_exemplar,
    chat_turn,
    looks_like_textual_tool_call,
    normalize_messages_for_provider,
    note_textual_tool_call,
    reset_tool_calling,
)
from conch.tooling import ToolRuntimeState


def _state():
    return ToolRuntimeState(all_tools=[], tool_map={}, tools=[])


def _base_messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


# ---------------------------------------------------------------------------
# looks_like_textual_tool_call
# ---------------------------------------------------------------------------

class TestLooksLikeTextualToolCall(unittest.TestCase):
    def test_bare_json_call(self):
        self.assertTrue(
            looks_like_textual_tool_call('{"name": "x", "arguments": {}}')
        )

    def test_tool_call_tag(self):
        self.assertTrue(
            looks_like_textual_tool_call('<tool_call>{"name": "x"}</tool_call>')
        )

    def test_function_calls_xml(self):
        self.assertTrue(
            looks_like_textual_tool_call('<function_calls><invoke name="x">')
        )

    def test_malformed_fenced_json_call(self):
        # Invalid JSON but clearly an attempted call (name + arguments hints).
        self.assertTrue(
            looks_like_textual_tool_call('```json\n{"name": "y", "arguments": {a}}\n```')
        )

    def test_ordinary_json_answer_not_flagged(self):
        self.assertFalse(looks_like_textual_tool_call('{"answer": 42}'))
        self.assertFalse(looks_like_textual_tool_call('{"result": [1, 2, 3]}'))

    def test_prose_not_flagged(self):
        self.assertFalse(looks_like_textual_tool_call("The set is {1, 2, 3}."))
        self.assertFalse(looks_like_textual_tool_call("Here are your files."))

    def test_empty_and_non_string(self):
        self.assertFalse(looks_like_textual_tool_call(""))
        self.assertFalse(looks_like_textual_tool_call(None))


# ---------------------------------------------------------------------------
# build_tool_call_exemplar
# ---------------------------------------------------------------------------

class TestBuildExemplar(unittest.TestCase):
    def test_ollama_exemplar_shape(self):
        ex = build_tool_call_exemplar("ollama")
        self.assertTrue(ex)
        # Contains a structured (native) tool call with dict arguments.
        calls = [m for m in ex if m.get("tool_calls")]
        self.assertEqual(len(calls), 1)
        args = calls[0]["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(args, dict)
        # And a paired tool result with tool_name linkage.
        tool_msgs = [m for m in ex if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("tool_name", tool_msgs[0])

    def test_custom_provider_gets_exemplar(self):
        self.assertTrue(build_tool_call_exemplar("custom"))

    def test_cloud_providers_get_nothing(self):
        self.assertEqual(build_tool_call_exemplar("openai"), [])
        self.assertEqual(build_tool_call_exemplar("anthropic"), [])
        self.assertEqual(build_tool_call_exemplar("cerebras"), [])


# ---------------------------------------------------------------------------
# apply_tool_call_scaffolding
# ---------------------------------------------------------------------------

class TestApplyScaffolding(unittest.TestCase):
    def test_exemplar_injected_after_system_for_ollama(self):
        base = _base_messages()
        out = apply_tool_call_scaffolding(list(base), "ollama", {}, _state())
        self.assertGreater(len(out), len(base))
        self.assertEqual(out[0], base[0], "leading system message stays first")
        self.assertTrue(any(m.get("tool_calls") for m in out))

    def test_no_injection_for_cloud_provider(self):
        base = _base_messages()
        out = apply_tool_call_scaffolding(list(base), "openai", {}, _state())
        self.assertEqual(out, base)

    def test_exemplar_can_be_disabled_by_config(self):
        base = _base_messages()
        out = apply_tool_call_scaffolding(
            list(base), "ollama", {"local_tool_exemplar": "false"}, _state()
        )
        self.assertEqual(out, base)

    def test_reminder_absent_below_threshold(self):
        s = _state()
        s.textual_tool_calls = DRIFT_REMINDER_THRESHOLD - 1
        out = apply_tool_call_scaffolding(_base_messages(), "ollama", {}, s)
        self.assertFalse(any(m.get("content") == TOOL_CALL_REMINDER for m in out))

    def test_reminder_present_at_threshold(self):
        s = _state()
        s.textual_tool_calls = DRIFT_REMINDER_THRESHOLD
        out = apply_tool_call_scaffolding(_base_messages(), "ollama", {}, s)
        self.assertTrue(any(m.get("content") == TOOL_CALL_REMINDER for m in out))

    def test_force_reminder_is_one_shot(self):
        s = _state()
        s.force_tool_reminder = True
        out = apply_tool_call_scaffolding(_base_messages(), "ollama", {}, s)
        self.assertTrue(any(m.get("content") == TOOL_CALL_REMINDER for m in out))
        self.assertFalse(s.force_tool_reminder, "force flag consumed after use")

    def test_no_chat_state_is_tolerated(self):
        out = apply_tool_call_scaffolding(_base_messages(), "ollama", {}, None)
        self.assertTrue(any(m.get("tool_calls") for m in out))  # exemplar only


# ---------------------------------------------------------------------------
# note_textual_tool_call
# ---------------------------------------------------------------------------

class TestNoteTextualToolCall(unittest.TestCase):
    def test_increments(self):
        s = _state()
        note_textual_tool_call(s)
        note_textual_tool_call(s)
        self.assertEqual(s.textual_tool_calls, 2)

    def test_none_tolerated(self):
        note_textual_tool_call(None)  # must not raise


# ---------------------------------------------------------------------------
# reset_tool_calling
# ---------------------------------------------------------------------------

class TestResetToolCalling(unittest.TestCase):
    def test_scrubs_textual_prose_keeps_structured_and_normal(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": '{"name": "foo", "arguments": {}}'},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "local_shell", "arguments": "{}"}},
            ]},
            {"role": "assistant", "content": "Here is your answer."},
        ]
        removed = reset_tool_calling(messages)
        self.assertEqual(removed, 1)
        self.assertEqual(len(messages), 4)
        # The structured call and the normal reply survive.
        self.assertTrue(any(m.get("tool_calls") for m in messages))
        self.assertTrue(any(m.get("content") == "Here is your answer." for m in messages))

    def test_structured_call_with_json_content_not_scrubbed(self):
        # An assistant message with real tool_calls is the *correct* form even
        # if its content happens to be JSON — never scrub it.
        messages = [
            {"role": "assistant", "content": '{"name": "x", "arguments": {}}',
             "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "x", "arguments": "{}"}}]},
        ]
        self.assertEqual(reset_tool_calling(messages), 0)

    def test_no_toxic_prose_is_noop(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "A normal reply."},
        ]
        self.assertEqual(reset_tool_calling(messages), 0)
        self.assertEqual(len(messages), 3)


# ---------------------------------------------------------------------------
# chat_turn integration: drift counting, no leak, structured replay
# ---------------------------------------------------------------------------

class _RecordingShell:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"text": "ok"}]}


def _run_turn(responses, chat_state=None, messages=None):
    it = iter(responses)
    messages = messages if messages is not None else [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "list files"},
    ]
    with patch("sys.stderr", io.StringIO()):
        reply, usage = chat_turn(
            config={"provider": "ollama", "chat_model": "qwen2.5-coder"},
            provider="ollama",
            raw_fn=lambda c, m, t: next(it),
            messages=messages,
            tools=[{"type": "function",
                    "function": {"name": "local_shell", "parameters": {}}}],
            tool_map={},
            builtin_clients={"local_shell": _RecordingShell()},
            max_tool_rounds=5,
            chat_state=chat_state,
        )
    return reply, messages


class TestChatTurnDrift(unittest.TestCase):
    def test_recovered_textual_call_counts_as_drift_and_stores_structured(self):
        s = _state()
        reply, messages = _run_turn([
            {"role": "assistant",
             "content": '{"name": "local_shell", "arguments": {"command": "ls"}}',
             "tool_calls": None, "_usage": {}, "_model": "m"},
            {"role": "assistant", "content": "Done.", "tool_calls": None,
             "_usage": {}, "_model": "m"},
        ], chat_state=s)
        self.assertEqual(reply, "Done.")
        self.assertGreaterEqual(s.textual_tool_calls, 1)
        # The recovered call is persisted as a STRUCTURED tool_calls message,
        # never as raw JSON prose.
        assistant_calls = [m for m in messages if m.get("tool_calls")]
        self.assertEqual(len(assistant_calls), 1)
        self.assertEqual(assistant_calls[0].get("content"), "")

    def test_unrecovered_textual_reply_counts_as_drift(self):
        # Names an unregistered tool: recovery declines it, it becomes the
        # reply. That is the compounding-drift case — it must be counted.
        s = _state()
        reply, _ = _run_turn([
            {"role": "assistant",
             "content": '{"name": "not_a_real_tool", "arguments": {"x": 1}}',
             "tool_calls": None, "_usage": {}, "_model": "m"},
        ], chat_state=s)
        self.assertEqual(s.textual_tool_calls, 1)

    def test_ordinary_reply_is_not_drift(self):
        s = _state()
        _run_turn([
            {"role": "assistant", "content": "The files are a.txt and b.txt.",
             "tool_calls": None, "_usage": {}, "_model": "m"},
        ], chat_state=s)
        self.assertEqual(s.textual_tool_calls, 0)

    def test_scaffolding_never_leaks_into_persisted_history(self):
        s = _state()
        _reply, messages = _run_turn([
            {"role": "assistant",
             "content": '{"name": "local_shell", "arguments": {"command": "ls"}}',
             "tool_calls": None, "_usage": {}, "_model": "m"},
            {"role": "assistant", "content": "Done.", "tool_calls": None,
             "_usage": {}, "_model": "m"},
        ], chat_state=s)
        blob = json.dumps(messages)
        self.assertNotIn("FORMAT EXAMPLE", blob)
        self.assertNotIn("(example)", blob)
        self.assertNotIn(TOOL_CALL_REMINDER, blob)

    def test_native_tool_calls_survive_ollama_normalization(self):
        # Regression: an assistant native tool_calls message must stay a
        # structured call on replay, not be flattened into prose.
        history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "ollama_0", "type": "function",
                 "function": {"name": "local_shell", "arguments": '{"command": "ls"}'}},
            ]},
            {"role": "tool", "tool_call_id": "ollama_0", "content": "a.txt"},
        ]
        out = normalize_messages_for_provider(history, "ollama")
        assistant = [m for m in out if m.get("tool_calls")]
        self.assertEqual(len(assistant), 1)
        self.assertIsInstance(
            assistant[0]["tool_calls"][0]["function"]["arguments"], dict
        )


# ---------------------------------------------------------------------------
# /resettools slash command
# ---------------------------------------------------------------------------

class TestResetToolsCommand(unittest.TestCase):
    def _run(self, cmd):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from conch.commands import handle_slash_command
            result = handle_slash_command(
                cmd, {"provider": "ollama"}, "ollama", "qwen2.5-coder",
                lambda v: None,
            )
        return result

    def test_resettools_returns_sentinel(self):
        self.assertEqual(self._run("/resettools"), "reset_tool_calling")

    def test_reset_alias_returns_sentinel(self):
        self.assertEqual(self._run("/reset"), "reset_tool_calling")

    def test_registered_in_slash_commands(self):
        from conch.commands import slash_command_names
        self.assertIn("/resettools", slash_command_names())


if __name__ == "__main__":
    unittest.main()
