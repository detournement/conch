"""Saving a conversation is incremental: per-message scrub cache, an FTS
index that touches only changed rows, and a merged, locked index.json.

These pin the correctness side of the optimisation: the bytes on disk are
exactly what a full scrub produces, edits and compactions are never served
stale scrub results, and the search index always matches a full rebuild.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch import conversations
from conch.conversations import (
    REDACTION_MARKER,
    ConversationManager,
    _clear_store_cache,
)

SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class _StateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        _clear_store_cache()
        self.addCleanup(_clear_store_cache)

    def _messages(self):
        return [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "deploy the kubernetes cluster ünïcode ✓"},
            {"role": "assistant", "content": f"token is {SECRET}",
             "tool_calls": [{"id": "c1", "type": "function", "function": {
                 "name": "run", "arguments": json.dumps({"cmd": f"echo {SECRET}"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": f"out {SECRET}\nline2"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "t1", "name": "read", "input": {"k": SECRET}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": f"x {SECRET}"},
                {"type": "text", "text": "user text stays"},
            ]},
            {"role": "assistant", "content": ""},
        ]

    def _fts_rows(self, manager, conv_id):
        conn = manager._search_index._connect()
        return sorted(conn.execute(
            "SELECT message_index, role, text FROM messages_fts WHERE conv_id = ?",
            (conv_id,),
        ).fetchall())

    def _rebuilt_rows(self, conv_id):
        """FTS rows the old full delete-and-reinsert indexing would write
        for the saved file."""
        saved = ConversationManager().load(conv_id)
        rows = []
        if saved.title and saved.title != "New conversation":
            rows.append((-1, "title", saved.title))
        for index, msg in enumerate(saved.messages):
            if msg.get("role") == "system":
                continue
            text = conversations._extract_searchable_text(msg)
            if text.strip():
                rows.append((index, msg.get("role", ""), text))
        return sorted(rows)


class TestStoredBytes(_StateCase):
    def test_file_is_byte_identical_to_full_scrub(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = self._messages()
        conv.usage = {"input_tokens": 3, "cost": 0.5}
        conv.previous_context = f"files: a.py {SECRET}"
        manager.save(conv)
        expected = json.dumps(conv.storage_copy().to_dict(), indent=2)
        self.assertEqual(conv.path.read_text(), expected)
        self.assertNotIn(SECRET, conv.path.read_text())
        # Cached second save: still identical.
        conv.messages.append({"role": "user", "content": "more"})
        manager.save(conv)
        self.assertEqual(
            conv.path.read_text(),
            json.dumps(conv.storage_copy().to_dict(), indent=2),
        )

    def test_empty_conversation_matches(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        self.assertEqual(
            conv.path.read_text(),
            json.dumps(conv.storage_copy().to_dict(), indent=2),
        )

    def test_unchanged_messages_are_not_rescrubbed(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = self._messages()
        manager.save(conv)
        conv.messages.append({"role": "assistant", "content": "new reply"})
        with mock.patch.object(
            conversations, "scrub_messages_for_storage",
            wraps=conversations.scrub_messages_for_storage,
        ) as scrub:
            manager.save(conv)
        self.assertEqual(scrub.call_count, 1)
        self.assertEqual(scrub.call_args[0][0], [conv.messages[-1]])

    def test_in_place_edit_is_rescrubbed(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = [{"role": "assistant", "content": "harmless"}]
        manager.save(conv)
        conv.messages[0]["content"] = f"now leaking {SECRET}"  # same dict, same index
        manager.save(conv)
        text = conv.path.read_text()
        self.assertNotIn(SECRET, text)
        self.assertIn(REDACTION_MARKER, text)
        conv.messages[0]["tool_calls"] = [{"function": {"name": "x", "arguments": SECRET}}]
        manager.save(conv)
        self.assertNotIn(SECRET, conv.path.read_text())

    def test_tuple_content_is_scrubbed_like_a_list(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        # Same JSON as a list: must never be served an unscrubbed entry.
        conv.messages = [{"role": "assistant", "content": ({"type": "text", "text": SECRET},)}]
        manager.save(conv)
        self.assertNotIn(SECRET, conv.path.read_text())
        conv.messages = [{"role": "assistant", "content": [{"type": "text", "text": SECRET}]}]
        manager.save(conv)
        self.assertNotIn(SECRET, conv.path.read_text())


class TestIncrementalSearchIndex(_StateCase):
    def setUp(self):
        super().setUp()
        if not ConversationManager()._search_index.available():
            self.skipTest("SQLite FTS5 unavailable")

    def test_append_keeps_existing_rows(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = self._messages()
        manager.save(conv)
        conn = manager._search_index._connect()
        before = dict(conn.execute(
            "SELECT message_index, fts_rowid FROM fts_rows WHERE conv_id = ?", (conv.id,)
        ))
        conv.messages.append({"role": "user", "content": "terraform plan"})
        manager.save(conv)
        after = dict(conn.execute(
            "SELECT message_index, fts_rowid FROM fts_rows WHERE conv_id = ?", (conv.id,)
        ))
        for index, rowid in before.items():
            self.assertEqual(after[index], rowid)
        self.assertIn(len(conv.messages) - 1, after)
        self.assertEqual(self._fts_rows(manager, conv.id), self._rebuilt_rows(conv.id))
        self.assertEqual([r["id"] for r in manager.search("terraform")], [conv.id])
        joined = " ".join(row[2] for row in self._fts_rows(manager, conv.id))
        self.assertNotIn(SECRET, joined)

    def test_compaction_rewrite_matches_full_rebuild(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = self._messages() + [
            {"role": "user", "content": f"old topic zebra {i}"} for i in range(20)
        ]
        manager.save(conv)
        tail = conv.messages[-3:]
        conv.messages.clear()
        conv.messages.extend(
            [{"role": "system", "content": "s"},
             {"role": "user", "content": "compacted summary giraffe"}] + tail
        )
        manager.save(conv)
        self.assertEqual(self._fts_rows(manager, conv.id), self._rebuilt_rows(conv.id))
        hits = manager.search("zebra")[0]["matches"]
        self.assertEqual(sorted(m["message_index"] for m in hits), [2, 3, 4])
        self.assertEqual([r["id"] for r in manager.search("giraffe")], [conv.id])
        # Only the title (taken from the first prompt) still mentions it.
        self.assertEqual(manager.search("kubernetes")[0]["matches"], [])

    def test_in_place_edit_and_retitle_update_rows(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = [{"role": "user", "content": "alpha"}]
        manager.save(conv)
        conv.messages[0]["content"] = "bravo"
        manager.rename(conv, "charlie title")
        self.assertEqual(manager.search("alpha"), [])
        self.assertEqual([r["id"] for r in manager.search("bravo")], [conv.id])
        self.assertEqual([r["id"] for r in manager.search("charlie")], [conv.id])
        self.assertEqual(self._fts_rows(manager, conv.id), self._rebuilt_rows(conv.id))

    def test_rows_from_an_older_writer_are_rebuilt(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = [{"role": "user", "content": "alpha"}]
        manager.save(conv)
        # An older conch re-indexes the conversation the legacy way:
        # delete-and-reinsert with fresh rowids, indexed_convs rewritten,
        # fts_rows/fts_state left untouched.
        conn = manager._search_index._connect()
        with conn:
            conn.execute("DELETE FROM messages_fts WHERE conv_id = ?", (conv.id,))
            conn.execute("INSERT INTO messages_fts VALUES (?, ?, ?, ?)",
                         ("other", 0, "user", "unrelated"))
            conn.execute("INSERT INTO messages_fts VALUES (?, ?, ?, ?)",
                         (conv.id, 0, "user", "alpha"))
            conn.execute("INSERT OR REPLACE INTO indexed_convs VALUES (?, ?)",
                         (conv.id, "legacy-stamp"))
        conv.messages.append({"role": "user", "content": "delta"})
        manager.save(conv)
        self.assertEqual(self._fts_rows(manager, conv.id), self._rebuilt_rows(conv.id))
        self.assertEqual(
            conn.execute("SELECT text FROM messages_fts WHERE conv_id = 'other'").fetchall(),
            [("unrelated",)],
        )

    def test_stale_rowid_map_falls_back_to_rebuild(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = [{"role": "user", "content": "alpha"}]
        manager.save(conv)
        conn = manager._search_index._connect()
        with conn:  # the map points at a row that is no longer ours
            conn.execute("UPDATE fts_rows SET fts_rowid = 999999 WHERE conv_id = ?", (conv.id,))
        conv.messages[0] = {"role": "user", "content": "echo"}
        manager.save(conv)
        self.assertEqual(self._fts_rows(manager, conv.id), self._rebuilt_rows(conv.id))

    def test_delete_clears_bookkeeping(self):
        manager = ConversationManager()
        conv = manager.create(model="m", provider="p")
        conv.messages = [{"role": "user", "content": "alpha"}]
        manager.save(conv)
        manager.delete(conv.id)
        conn = manager._search_index._connect()
        for table in ("messages_fts", "fts_rows", "fts_state", "indexed_convs"):
            self.assertEqual(
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE conv_id = ?",
                             (conv.id,)).fetchone()[0], 0, table)


class TestIndexMerge(_StateCase):
    def _ids_on_disk(self):
        path = Path(self._tmp.name) / "conch" / "conversations" / "index.json"
        return {item["id"] for item in json.loads(path.read_text())["conversations"]}

    def test_two_shells_do_not_clobber_each_other(self):
        shell_a = ConversationManager()
        shell_b = ConversationManager()  # loaded before A wrote anything
        conv_a = shell_a.create(model="m", provider="p")
        conv_b = shell_b.create(model="m", provider="p")
        conv_a.messages = [{"role": "user", "content": "a"}]
        shell_a.save(conv_a)  # A's in-memory index predates B's entry
        self.assertEqual(self._ids_on_disk(), {conv_a.id, conv_b.id})
        self.assertEqual({e["id"] for e in shell_a.list_all()}, {conv_a.id, conv_b.id})

    def test_delete_keeps_other_shells_entries(self):
        shell_a = ConversationManager()
        conv_a = shell_a.create(model="m", provider="p")
        shell_b = ConversationManager()
        conv_b = shell_b.create(model="m", provider="p")
        shell_a.delete(conv_a.id)
        self.assertEqual(self._ids_on_disk(), {conv_b.id})

    def test_corrupt_index_falls_back_to_memory(self):
        manager = ConversationManager()
        conv_a = manager.create(model="m", provider="p")
        path = Path(self._tmp.name) / "conch" / "conversations" / "index.json"
        path.write_text("{not json")
        conv_b = manager.create(model="m", provider="p")
        self.assertEqual(self._ids_on_disk(), {conv_a.id, conv_b.id})

    def test_lock_file_is_private(self):
        manager = ConversationManager()
        manager.create(model="m", provider="p")
        lock = Path(self._tmp.name) / "conch" / "conversations" / "index.json.lock"
        if lock.exists():
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
