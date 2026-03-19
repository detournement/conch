import unittest

from conch.runtime import extract_textual_tool_use_blocks, sanitize_anthropic_messages


class RuntimeTests(unittest.TestCase):
    def test_textual_tool_use_recovery(self):
        text = "({'type': 'tool_use', 'id': 'toolu_x', 'name': 'local_shell', 'input': {'command': 'echo hi'}})"
        blocks = extract_textual_tool_use_blocks(text)
        self.assertEqual(blocks[0]["name"], "local_shell")
        self.assertEqual(blocks[0]["input"]["command"], "echo hi")

    def test_sanitize_anthropic_messages_drops_orphans(self):
        messages = [
            {"role": "system", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "tool", "input": {}}]},
            {"role": "assistant", "content": "later"},
        ]
        sanitize_anthropic_messages(messages)
        self.assertEqual(messages[-1]["content"], "later")
        self.assertEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
