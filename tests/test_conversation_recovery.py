"""A corrupt conversation file must never prevent startup.

Fixtures in tests/fixtures/corrupt_conversations/ reproduce each corruption
class seen or feared in production:

- extra_data.json  — the real incident: a complete JSON document followed by
  stale trailing bytes (torn in-place write). Salvageable: the valid prefix
  is the conversation.
- truncated.json   — write torn mid-object; no valid prefix. Quarantine.
- empty.json       — zero bytes. Quarantine.
- wrong_shape.json — the valid prefix decodes but is not a conversation
  (no id/messages). Salvage must be rejected. Quarantine.

In every case the startup path (get_most_recent) continues to the next
conversation or a fresh one, the original bytes are preserved at
<name>.json.corrupt-<timestamp>, and one warning line names that file.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import ConversationManager, _state_dir

FIXTURES = Path(__file__).parent / "fixtures" / "corrupt_conversations"


def _install_corrupt(fixture: str, conv_id: str, updated_at: str = "2026-09-01T10:05:00"):
    """Copy a corruption fixture into the isolated XDG state dir and index it."""
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture, state / f"{conv_id}.json")
    index_path = state / "index.json"
    index = (
        json.loads(index_path.read_text())
        if index_path.exists()
        else {"schema_version": 2, "conversations": []}
    )
    index["conversations"].insert(0, {
        "id": conv_id,
        "title": "corrupt fixture",
        "model": "m",
        "provider": "p",
        "updated_at": updated_at,
        "message_count": 0,
    })
    index_path.write_text(json.dumps(index))


class _IsolatedState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": self._tmp.name}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _load_with_stderr(self, mgr, conv_id):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            conv = mgr.load(conv_id)
        return conv, stderr.getvalue()

    def _quarantine_files(self):
        return sorted(_state_dir().glob("*.corrupt-*"))


class TestExtraDataSalvage(_IsolatedState):
    """The real incident: valid prefix + stale trailing bytes → salvage."""

    def test_valid_prefix_is_salvaged(self):
        _install_corrupt("extra_data.json", "aaaa1111")
        mgr = ConversationManager()
        conv, warning = self._load_with_stderr(mgr, "aaaa1111")
        self.assertIsNotNone(conv)
        self.assertEqual(conv.id, "aaaa1111")
        self.assertEqual(len(conv.messages), 3)
        self.assertEqual(
            conv.messages[1]["content"], "the message written before the outage"
        )
        mgr.close()

    def test_salvage_rewrites_file_and_keeps_original(self):
        _install_corrupt("extra_data.json", "aaaa1111")
        mgr = ConversationManager()
        with contextlib.redirect_stderr(io.StringIO()):
            mgr.load("aaaa1111")
        # The live file is now clean JSON.
        on_disk = json.loads((_state_dir() / "aaaa1111.json").read_text())
        self.assertEqual(on_disk["id"], "aaaa1111")
        # The corrupt original is preserved byte-for-byte.
        backups = self._quarantine_files()
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            backups[0].read_bytes(), (FIXTURES / "extra_data.json").read_bytes()
        )
        mgr.close()

    def test_salvage_warning_names_backup_file(self):
        _install_corrupt("extra_data.json", "aaaa1111")
        mgr = ConversationManager()
        _conv, warning = self._load_with_stderr(mgr, "aaaa1111")
        backup = self._quarantine_files()[0]
        self.assertIn(str(backup), warning)
        self.assertIn("aaaa1111", warning)
        mgr.close()

    def test_startup_resumes_salvaged_conversation(self):
        _install_corrupt("extra_data.json", "aaaa1111")
        mgr = ConversationManager()
        with contextlib.redirect_stderr(io.StringIO()):
            conv = mgr.get_most_recent()
        self.assertIsNotNone(conv)
        self.assertEqual(conv.id, "aaaa1111")
        mgr.close()


class TestQuarantine(_IsolatedState):
    """Unsalvageable corruption → quarantine, drop from index, continue."""

    CASES = [
        ("truncated.json", "bbbb2222"),
        ("empty.json", "cccc3333"),
        ("wrong_shape.json", "dddd4444"),
    ]

    def test_load_returns_none_and_quarantines(self):
        for fixture, conv_id in self.CASES:
            with self.subTest(fixture=fixture):
                _install_corrupt(fixture, conv_id)
                mgr = ConversationManager()
                conv, warning = self._load_with_stderr(mgr, conv_id)
                self.assertIsNone(conv)
                self.assertFalse((_state_dir() / f"{conv_id}.json").exists())
                backups = [
                    p for p in self._quarantine_files() if conv_id in p.name
                ]
                self.assertEqual(len(backups), 1)
                self.assertEqual(
                    backups[0].read_bytes(),
                    (FIXTURES / fixture).read_bytes(),
                    "quarantine must preserve the original bytes",
                )
                self.assertIn(str(backups[0]), warning)
                self.assertNotIn(
                    conv_id, [e["id"] for e in mgr.list_all()],
                    "quarantined conversation must leave the index",
                )
                mgr.close()

    def test_startup_falls_through_to_next_conversation(self):
        # An older, healthy conversation exists behind the corrupt one.
        mgr = ConversationManager()
        healthy = mgr.create(model="m", provider="p")
        healthy.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "healthy history"},
        ]
        mgr.save(healthy)
        mgr.close()
        _install_corrupt("truncated.json", "bbbb2222",
                         updated_at="2099-01-01T00:00:00")

        mgr = ConversationManager()
        self.assertEqual(mgr.list_all()[0]["id"], "bbbb2222")
        with contextlib.redirect_stderr(io.StringIO()):
            conv = mgr.get_most_recent()
        self.assertIsNotNone(conv)
        self.assertEqual(conv.id, healthy.id)
        mgr.close()

    def test_startup_with_only_corrupt_conversations_returns_none(self):
        _install_corrupt("truncated.json", "bbbb2222")
        _install_corrupt("empty.json", "cccc3333")
        mgr = ConversationManager()
        with contextlib.redirect_stderr(io.StringIO()):
            conv = mgr.get_most_recent()
        self.assertIsNone(conv)  # chat_loop then creates a fresh conversation
        mgr.close()


class TestSearchIndexToleratesQuarantine(_IsolatedState):
    def test_search_rebuild_survives_corrupt_file(self):
        mgr = ConversationManager()
        conv = mgr.create(model="m", provider="p")
        conv.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "searchable kubernetes question"},
        ]
        mgr.save(conv)
        mgr.close()
        _install_corrupt("truncated.json", "bbbb2222",
                         updated_at="2099-01-01T00:00:00")

        mgr = ConversationManager()
        with contextlib.redirect_stderr(io.StringIO()):
            results = mgr.search("kubernetes")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], conv.id)
        mgr.close()


if __name__ == "__main__":
    unittest.main()
