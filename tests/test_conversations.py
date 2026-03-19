import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import ConversationManager


class ConversationTests(unittest.TestCase):
    def test_structured_messages_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                manager = ConversationManager()
                conv = manager.create(model="m", provider="p")
                conv.messages = [
                    {"role": "system", "content": "s"},
                    {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]},
                ]
                manager.save(conv)
                loaded = manager.load(conv.id)
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.messages[1]["content"][0]["text"], "hello")
                self.assertEqual(loaded.messages[2]["content"][0]["type"], "tool_result")


if __name__ == "__main__":
    unittest.main()
