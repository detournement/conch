"""The conversations page (conch convos, /convos): filtering, ordering, the
delete guard, and rename/fork/export, driven through ConversationsPage
without a terminal."""

import os
import stat
import tempfile
import unittest
from unittest import mock

from conch.conversations import ConversationManager
from conch.convo_browser import ConversationsPage


class ConversationsPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.manager = ConversationManager()
        self.addCleanup(self.manager.close)

    def seed(self, title, *texts, model="m", usage=None):
        conv = self.manager.create(model=model, provider="p")
        conv.title = title
        conv.messages = [{"role": "system", "content": "s"}] + [
            {"role": "user", "content": text} for text in texts
        ]
        conv.usage = dict(usage or {})
        self.manager.save(conv)
        return conv

    def titles(self, page):
        return [row.entry["title"] for row in page.rows]

    def test_every_word_must_match_title_text_or_model(self):
        self.seed("deploy notes", "docker compose up")
        self.seed("cluster", "kubernetes ingress with docker")
        self.seed("misc", "kubernetes only", model="docker-tuned")
        page = ConversationsPage(self.manager)
        page.search.text = "docker kubernetes"
        self.assertEqual(sorted(self.titles(page)), ["cluster", "misc"])
        page.search.text = ""
        self.assertEqual(len(page.rows), 3)

    def test_searching_ranks_by_relevance_then_clearing_restores_recency(self):
        self.seed("one mention", "docker")
        self.seed("many mentions", "docker docker", "docker again", "more docker")
        self.seed("unrelated", "hello")
        page = ConversationsPage(self.manager)
        page.search.text = "docker"
        self.assertEqual(page.sort, "relevance")
        self.assertEqual(self.titles(page), ["many mentions", "one mention"])
        page.search.text = ""
        self.assertEqual(page.sort, "recent")
        self.assertEqual(self.titles(page), ["unrelated", "many mentions", "one mention"])

    def test_cost_sort_puts_the_most_expensive_first(self):
        self.seed("cheap", "a", usage={"cost": 0.01, "turns": 1, "api_turns": 1})
        self.seed("untracked", "b")
        self.seed("pricey", "c", usage={"cost": 2.5, "turns": 3, "api_turns": 3})
        page = ConversationsPage(self.manager)
        page.cycle_sort()
        self.assertEqual(page.sort, "cost")
        self.assertEqual(self.titles(page), ["pricey", "cheap", "untracked"])

    def test_the_conversation_in_use_cannot_be_deleted(self):
        current = self.seed("in use", "x")
        page = ConversationsPage(self.manager, current_id=current.id)
        page.selected = self.titles(page).index("in use")
        page.ask_delete()
        self.assertEqual(page.mode, "list")
        self.assertIsNotNone(self.manager.load(current.id))

    def test_confirmed_delete_removes_the_conversation(self):
        keep = self.seed("keep", "x")
        drop = self.seed("drop", "y")
        page = ConversationsPage(self.manager, current_id=keep.id)
        page.selected = self.titles(page).index("drop")
        page.ask_delete()
        self.assertEqual(page.mode, "confirm")
        page.delete()
        self.assertIsNone(self.manager.load(drop.id))
        self.assertEqual(self.titles(page), ["keep"])

    def test_rename_saves_through_the_manager_and_escape_keeps_the_title(self):
        conv = self.seed("old title", "x")
        page = ConversationsPage(self.manager)
        page.start_rename()
        self.assertEqual(page.mode, "rename")
        self.assertEqual(page.rename_input.text, "old title")
        page.rename_input.text = "discarded"
        page.finish_rename(save=False)
        self.assertEqual(page.mode, "list")
        self.assertEqual(self.manager.load(conv.id).title, "old title")
        page.start_rename()
        page.rename_input.text = "  new   title "
        page.finish_rename(save=True)
        self.assertEqual(self.manager.load(conv.id).title, "new title")
        self.assertEqual(self.titles(page), ["new title"])
        page.start_rename()
        page.rename_input.text = "   "
        page.finish_rename(save=True)
        self.assertEqual(self.manager.load(conv.id).title, "new title")

    def test_fork_creates_and_selects_the_copy(self):
        original = self.seed("plan", "x", usage={"cost": 1.0, "turns": 1, "api_turns": 1})
        self.seed("other", "y")
        page = ConversationsPage(self.manager)
        page.selected = self.titles(page).index("plan")
        page.fork()
        selected = page.current_row()
        self.assertEqual(selected.entry["title"], "plan (fork)")
        self.assertNotEqual(selected.id, original.id)
        self.assertIn(selected.id, page.notice)
        forked = self.manager.load(selected.id)
        self.assertEqual(forked.messages, self.manager.load(original.id).messages)
        self.assertEqual(forked.usage, {})
        self.assertEqual(len(page.rows), 3)

    def test_export_writes_a_private_file_without_overwriting(self):
        self.seed("notes", "hello there")
        out = tempfile.TemporaryDirectory()
        self.addCleanup(out.cleanup)
        page = ConversationsPage(self.manager)
        page.export_dir = out.name
        page.export()
        page.export()
        files = sorted(os.listdir(out.name))
        self.assertEqual(len(files), 2)
        self.assertTrue(page.notice.endswith("-1.md"))
        self.assertTrue(any(os.path.join(out.name, name) in page.notice for name in files))
        for name in files:
            path = os.path.join(out.name, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as handle:
                self.assertIn("hello there", handle.read())


if __name__ == "__main__":
    unittest.main()
