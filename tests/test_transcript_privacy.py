"""Transcript privacy (review finding F12): conversation files and their
directory are owner-only, and credential-shaped model/tool output is
scrubbed before it is written to disk or into the search index, while
the in-memory conversation keeps the original for the running session."""

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import (
    REDACTION_MARKER,
    Conversation,
    ConversationManager,
    scrub_messages_for_storage,
)

GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
OPENAI_KEY = "sk-" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2H1g0F9e8D7c6"


class _StateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"XDG_STATE_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conv_dir = Path(self._tmp.name) / "conch" / "conversations"

    @staticmethod
    def _mode(path):
        return stat.S_IMODE(os.stat(path).st_mode)


class TestPermissions(_StateCase):
    def test_new_transcript_and_directory_are_owner_only(self):
        old_umask = os.umask(0o022)  # the permissive default most users have
        try:
            manager = ConversationManager()
            conv = manager.create(model="m", provider="p")
            conv.messages = [{"role": "user", "content": "hello"}]
            manager.save(conv)
        finally:
            os.umask(old_umask)
        self.assertEqual(self._mode(conv.path), 0o600)
        self.assertEqual(self._mode(self.conv_dir / "index.json"), 0o600)
        self.assertEqual(self._mode(self.conv_dir), 0o700)
        if manager._search_index.available():
            self.assertEqual(self._mode(self.conv_dir / "search.db"), 0o600)

    def test_existing_world_readable_transcript_is_tightened_on_save(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        os.chmod(conv.path, 0o644)
        os.chmod(self.conv_dir, 0o755)
        conv.messages = [{"role": "user", "content": "again"}]
        manager.save(conv)
        self.assertEqual(self._mode(conv.path), 0o600)
        self.assertEqual(self._mode(self.conv_dir), 0o700)

    def test_no_temp_file_is_left_world_readable(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        manager.save(conv)
        leftovers = [p for p in self.conv_dir.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftovers, [])


class TestScrubbing(_StateCase):
    def test_model_and_tool_output_scrubbed_on_disk_not_in_memory(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "show me the env"},
            {
                "role": "assistant",
                "content": f"Your token is {GITHUB_TOKEN}, keep it safe.",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {
                        "name": "local_shell",
                        "arguments": json.dumps({
                            "command": f"curl -H 'Authorization: token {GITHUB_TOKEN}' https://api.github.com",
                        }),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "c1",
             "content": f"AWS_ACCESS_KEY_ID={AWS_KEY}\nOPENAI_API_KEY={OPENAI_KEY}\n"},
        ]
        manager.save(conv)

        on_disk = conv.path.read_text()
        for secret in (GITHUB_TOKEN, AWS_KEY, OPENAI_KEY):
            self.assertNotIn(secret, on_disk)
        self.assertIn(REDACTION_MARKER, on_disk)
        # The running session still has the originals.
        self.assertIn(GITHUB_TOKEN, conv.messages[2]["content"])
        self.assertIn(AWS_KEY, conv.messages[3]["content"])
        # What comes back from disk is the scrubbed form.
        loaded = manager.load(conv.id)
        self.assertNotIn(GITHUB_TOKEN, loaded.messages[2]["content"])
        self.assertIn("keep it safe", loaded.messages[2]["content"])
        self.assertNotIn(
            GITHUB_TOKEN,
            loaded.messages[2]["tool_calls"][0]["function"]["arguments"],
        )

    def test_search_index_never_sees_the_secret(self):
        manager = ConversationManager()
        if not manager._search_index.available():
            self.skipTest("sqlite FTS unavailable")
        conv = manager.create(model="m", provider="p")
        conv.messages = [
            {"role": "user", "content": "what is in .env"},
            {"role": "tool", "tool_call_id": "c1",
             "content": f"OPENAI_API_KEY={OPENAI_KEY}"},
        ]
        manager.save(conv)
        conn = sqlite3.connect(str(self.conv_dir / "search.db"))
        try:
            rows = conn.execute("SELECT text FROM messages_fts").fetchall()
        finally:
            conn.close()
        joined = "\n".join(row[0] for row in rows)
        self.assertNotIn(OPENAI_KEY, joined)
        self.assertIn("what is in .env", joined)

    def test_anthropic_tool_result_blocks_are_scrubbed_user_text_kept(self):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": f"my own note {AWS_KEY}"},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": f"AWS_ACCESS_KEY_ID={AWS_KEY}"},
            ]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "done"},
                {"type": "tool_use", "id": "t2", "name": "local_shell",
                 "input": {"command": f"export GH={GITHUB_TOKEN}"}},
            ]},
        ]
        scrubbed = scrub_messages_for_storage(messages)
        self.assertIn(AWS_KEY, scrubbed[0]["content"][0]["text"],
                      "the user's own words are not rewritten")
        self.assertNotIn(AWS_KEY, scrubbed[0]["content"][1]["content"])
        self.assertNotIn(GITHUB_TOKEN,
                         scrubbed[1]["content"][1]["input"]["command"])
        # Originals untouched.
        self.assertIn(GITHUB_TOKEN, messages[1]["content"][1]["input"]["command"])

    def test_clean_messages_are_unchanged(self):
        messages = [
            {"role": "assistant", "content": "ls -la lists hidden files too"},
            {"role": "tool", "tool_call_id": "x", "content": "total 0"},
            {"role": "user", "content": "thanks"},
        ]
        self.assertEqual(scrub_messages_for_storage(messages), messages)

    def test_conversation_save_without_manager_also_scrubs(self):
        conv = Conversation(id="abc12345", title="t", model="m", provider="p",
                            messages=[{"role": "assistant",
                                       "content": f"token {GITHUB_TOKEN}"}])
        conv.save()
        self.assertNotIn(GITHUB_TOKEN, conv.path.read_text())
        self.assertEqual(self._mode(conv.path), 0o600)


if __name__ == "__main__":
    unittest.main()
