import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import ConversationManager, _extract_snippet


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


class SearchTests(unittest.TestCase):
    def _make_manager(self, tmp):
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
            mgr = ConversationManager()
            c1 = mgr.create(model="m", provider="p")
            c1.messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "how do I deploy to kubernetes"},
                {"role": "assistant", "content": "Use kubectl apply -f deployment.yaml"},
                {"role": "user", "content": "what about docker compose"},
                {"role": "assistant", "content": "Run docker compose up -d"},
            ]
            mgr.save(c1)

            c2 = mgr.create(model="m", provider="p")
            c2.messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "tell me about python packaging"},
                {"role": "assistant", "content": "Use pyproject.toml with setuptools or hatch"},
            ]
            mgr.save(c2)

            c3 = mgr.create(model="m", provider="p")
            c3.messages = [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            mgr.save(c3)
            return mgr

    def test_search_finds_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("kubernetes")
                self.assertEqual(len(results), 1)
                self.assertIn("kubernetes", results[0]["matches"][0]["snippet"].lower())

    def test_search_multiple_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("docker compose")
                self.assertGreaterEqual(len(results), 1)

    def test_search_across_conversations(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("python")
                self.assertEqual(len(results), 1)

    def test_search_no_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("xyznonexistent")
                self.assertEqual(len(results), 0)

    def test_search_empty_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("")
                self.assertEqual(len(results), 0)

    def test_search_skips_system_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("system prompt")
                for r in results:
                    for m in r["matches"]:
                        self.assertNotEqual(m["role"], "system")

    def test_search_sorted_by_relevance(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("docker")
                if len(results) > 1:
                    self.assertGreaterEqual(results[0]["score"], results[1]["score"])

    def test_search_max_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("hello", max_results=1)
                self.assertLessEqual(len(results), 1)

    def test_search_includes_snippets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                results = mgr.search("kubectl")
                self.assertGreater(len(results), 0)
                self.assertGreater(len(results[0]["matches"]), 0)
                self.assertTrue(results[0]["matches"][0]["snippet"])

    def test_search_title_boost(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_manager(tmp)
                c = mgr.create(model="m", provider="p")
                c.title = "kubernetes deployment guide"
                c.messages = [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "short message"},
                ]
                mgr.save(c)
                results = mgr.search("kubernetes")
                self.assertGreater(len(results), 0)


class TestExtractSnippet(unittest.TestCase):
    def test_keyword_centered(self):
        text = "The quick brown fox jumps over the lazy dog near the kubernetes cluster"
        snippet = _extract_snippet(text, ["kubernetes"], context_chars=60)
        self.assertIn("kubernetes", snippet)

    def test_ellipsis_for_long_text(self):
        text = "x" * 50 + "TARGET" + "y" * 200
        snippet = _extract_snippet(text, ["target"], context_chars=80)
        self.assertIn("...", snippet)

    def test_short_text_no_ellipsis(self):
        text = "short text here"
        snippet = _extract_snippet(text, ["short"], context_chars=100)
        self.assertNotIn("...", snippet)

    def test_no_match_returns_start(self):
        text = "no keywords here at all"
        snippet = _extract_snippet(text, ["zzz"], context_chars=100)
        self.assertTrue(snippet.startswith("no keywords"))


if __name__ == "__main__":
    unittest.main()
