"""Tests for conch.runtime — message normalization and context compression."""

import unittest

from conch.runtime import (
    compress_context,
    estimate_tokens,
    normalize_messages_for_provider,
    normalize_messages_on_switch,
    sanitize_anthropic_messages,
    extract_textual_tool_use_blocks,
)


class TestEstimateTokens(unittest.TestCase):
    def test_simple_string(self):
        msgs = [{"role": "user", "content": "hello"}]
        tokens = estimate_tokens(msgs)
        self.assertGreater(tokens, 0)

    def test_with_tools(self):
        msgs = [{"role": "user", "content": "hi"}]
        tools = [{"function": {"name": "test", "parameters": {}}}]
        with_tools = estimate_tokens(msgs, tools)
        without = estimate_tokens(msgs)
        self.assertGreater(with_tools, without)

    def test_structured_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        tokens = estimate_tokens(msgs)
        self.assertGreater(tokens, 0)


class TestCompressContext(unittest.TestCase):
    def test_short_context_unchanged(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = compress_context(msgs, None, "openai")
        self.assertEqual(len(result), len(msgs))

    def test_long_context_compressed(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(100):
            msgs.append({"role": "user", "content": "x" * 10000})
            msgs.append({"role": "assistant", "content": "y" * 10000})
        result = compress_context(msgs, None, "ollama")
        self.assertLess(len(result), len(msgs))

    def test_keeps_system_and_recent(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(50):
            msgs.append({"role": "user", "content": "x" * 3000})
            msgs.append({"role": "assistant", "content": "y" * 3000})
        result = compress_context(msgs, None, "ollama")
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[-1]["role"], "assistant")


class TestNormalizeMessagesForProvider(unittest.TestCase):
    def test_anthropic_passthrough(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        result = normalize_messages_for_provider(msgs, "anthropic")
        self.assertIs(result, msgs)

    def test_drops_tool_role(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "1", "content": "result"},
        ]
        result = normalize_messages_for_provider(msgs, "openai")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "hi")

    def test_drops_empty_tool_use_blocks(self):
        msgs = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "test"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]},
        ]
        result = normalize_messages_for_provider(msgs, "openai")
        self.assertEqual(len(result), 0)

    def test_extracts_text_from_structured(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "Here is the answer"},
                {"type": "tool_use", "id": "1", "name": "test"},
            ]},
        ]
        result = normalize_messages_for_provider(msgs, "openai")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "Here is the answer")

    def test_drops_empty_assistant_with_tool_calls(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        ]
        result = normalize_messages_for_provider(msgs, "openai")
        self.assertEqual(len(result), 0)


class TestNormalizeMessagesOnSwitch(unittest.TestCase):
    def test_preserves_system_and_text(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        normalize_messages_on_switch(msgs, "openai")
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["role"], "system")

    def test_drops_tool_pairs(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "t"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]},
            {"role": "assistant", "content": "done"},
        ]
        normalize_messages_on_switch(msgs, "openai")
        roles = [m["role"] for m in msgs]
        self.assertNotIn("tool", roles)
        self.assertEqual(msgs[-1]["content"], "done")


class TestSanitizeAnthropicMessages(unittest.TestCase):
    def test_removes_orphaned_tool_use(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "t"}]},
        ]
        sanitize_anthropic_messages(msgs)
        self.assertEqual(len(msgs), 2)

    def test_keeps_matched_pairs(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "t"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]},
        ]
        sanitize_anthropic_messages(msgs)
        self.assertEqual(len(msgs), 3)


class TestExtractTextualToolUse(unittest.TestCase):
    def test_returns_none_for_plain_text(self):
        self.assertIsNone(extract_textual_tool_use_blocks("just text"))

    def test_parses_xml_format(self):
        text = '<tool_called name="local_shell" args=\'{"command": "ls"}\'  />'
        result = extract_textual_tool_use_blocks(text)
        self.assertIsNotNone(result)
        self.assertEqual(result[0]["name"], "local_shell")

    def test_returns_none_for_empty(self):
        self.assertIsNone(extract_textual_tool_use_blocks(""))

    def test_returns_none_for_non_string(self):
        self.assertIsNone(extract_textual_tool_use_blocks(None))


if __name__ == "__main__":
    unittest.main()
