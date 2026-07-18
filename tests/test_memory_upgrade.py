"""Tests for the memory upgrade (plan 2.8): always-loaded facts file,
FTS5-ranked memory recall, and the FTS5 conversation search index."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import ConversationManager, SearchIndex, _fts_match_expression
from conch.memory import (
    FACTS_MAX_CHARS,
    MemoryStore,
    append_fact,
    facts_path,
    load_facts,
)


class TempDirsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)


class TestFactsFile(TempDirsTestCase):
    def test_no_file_empty(self):
        self.assertEqual(load_facts(), "")

    def test_append_and_load(self):
        self.assertTrue(append_fact("the prod server is athena"))
        self.assertTrue(append_fact("deploys go through CI only"))
        facts = load_facts()
        self.assertIn("Standing facts", facts)
        self.assertIn("- the prod server is athena", facts)
        self.assertIn("- deploys go through CI only", facts)

    def test_empty_fact_rejected(self):
        self.assertFalse(append_fact("   "))

    def test_bounded_injection(self):
        facts_path().parent.mkdir(parents=True, exist_ok=True)
        facts_path().write_text("x" * (FACTS_MAX_CHARS * 3))
        facts = load_facts()
        self.assertLess(len(facts), FACTS_MAX_CHARS + 200)
        self.assertIn("truncated", facts)

    def test_injected_into_system_prompt(self):
        append_fact("kubernetes cluster lives at 10.0.0.5")
        from conch.app import _build_system_prompt
        prompt = _build_system_prompt("base")
        self.assertIn("kubernetes cluster lives at 10.0.0.5", prompt)


class TestMemoryFtsRecall(TempDirsTestCase):
    def _store(self):
        store = MemoryStore()
        store.add("user prefers ripgrep over grep")
        store.add("the staging database is postgres on port 5433")
        store.add("likes short answers")
        return store

    def test_relevant_entry_recalled(self):
        store = self._store()
        context = store.build_context("what port does the staging database use?")
        self.assertIn("postgres on port 5433", context)

    def test_prefix_matching_via_fts(self):
        store = self._store()
        # "databases" stems to prefix-match "database"? No — but "postgre"
        # should prefix-match "postgres" with the "kw"* expression.
        context = store.build_context("postgre settings")
        self.assertIn("postgres", context)

    def test_no_match_empty(self):
        store = self._store()
        self.assertEqual(store.build_context("zebra unicorns"), "")

    def test_empty_query_empty(self):
        store = self._store()
        self.assertEqual(store.build_context("   "), "")

    def test_fallback_when_fts_unavailable(self):
        store = self._store()
        with mock.patch.object(MemoryStore, "_fts_rank", return_value=None):
            context = store.build_context("staging database port")
        self.assertIn("postgres on port 5433", context)

    def test_limit_respected(self):
        store = MemoryStore()
        for i in range(10):
            store.add(f"database note number {i}")
        context = store.build_context("database", )
        entries = [l for l in context.splitlines() if l.startswith("- ")]
        self.assertLessEqual(len(entries), 5)


class TestFtsMatchExpression(unittest.TestCase):
    def test_keywords_quoted_and_ored(self):
        self.assertEqual(_fts_match_expression("docker compose"),
                         '"docker"* OR "compose"*')

    def test_quotes_escaped(self):
        self.assertEqual(_fts_match_expression('say "hi"'),
                         '"say"* OR """hi"""*')

    def test_empty(self):
        self.assertEqual(_fts_match_expression("   "), "")


class TestConversationFtsIndex(TempDirsTestCase):
    def _manager_with_convs(self):
        mgr = ConversationManager()
        c1 = mgr.create(model="m", provider="p")
        c1.messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "how do I deploy to kubernetes"},
            {"role": "assistant", "content": "Use kubectl apply"},
        ]
        mgr.save(c1)
        c2 = mgr.create(model="m", provider="p")
        c2.messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "python packaging question"},
            {"role": "assistant", "content": "use pyproject.toml"},
        ]
        mgr.save(c2)
        return mgr, c1, c2

    def test_fts_index_used_for_search(self):
        mgr, c1, _ = self._manager_with_convs()
        self.assertTrue(mgr._search_index.available())
        results = mgr.search("kubernetes")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], c1.id)
        self.assertIn("kubernetes", results[0]["matches"][0]["snippet"].lower())

    def test_linear_scan_not_used_when_fts_available(self):
        mgr, _, _ = self._manager_with_convs()
        with mock.patch.object(
            ConversationManager, "_search_linear",
            side_effect=AssertionError("linear scan must not run"),
        ):
            results = mgr.search("kubernetes")
        self.assertEqual(len(results), 1)

    def test_delete_removes_from_index(self):
        mgr, c1, _ = self._manager_with_convs()
        mgr.delete(c1.id)
        self.assertEqual(mgr.search("kubernetes"), [])

    def test_lazy_sync_indexes_conversations_from_other_sessions(self):
        mgr, _, _ = self._manager_with_convs()
        # A second manager (fresh index handle, same state dir) — simulates
        # another session having written conversations.
        other = ConversationManager()
        c3 = other.create(model="m", provider="p")
        c3.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "terraform apply questions"},
        ]
        # Write the file + index entry but bypass FTS indexing to simulate
        # a writer without the index.
        c3.save()
        other._upsert_index_entry(c3)
        mgr._index = mgr._load_index()  # pick up the new index file
        results = mgr.search("terraform")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], c3.id)

    def test_title_match_boosts(self):
        mgr, _, _ = self._manager_with_convs()
        c = mgr.create(model="m", provider="p")
        c.title = "kubernetes deployment guide"
        c.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "short message"},
        ]
        mgr.save(c)
        results = mgr.search("kubernetes")
        self.assertEqual(len(results), 2)

    def test_fts_unavailable_falls_back_to_linear(self):
        mgr, _, _ = self._manager_with_convs()
        with mock.patch.object(SearchIndex, "available", return_value=False):
            results = mgr.search("kubernetes")
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
