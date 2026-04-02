import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conch.conversations import ConversationManager, _extract_searchable_text, _extract_snippet


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


class TestSearchToolContent(unittest.TestCase):
    """Tests that search finds content inside tool calls, tool results,
    and Anthropic-style structured messages."""

    def _make_tool_conversation(self, tmp):
        with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
            mgr = ConversationManager()
            conv = mgr.create(model="m", provider="p")
            conv.messages = [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "create a jira ticket for the billing bug"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "JIRA_CREATE_ISSUE",
                                "arguments": '{"project": "BILL", "summary": "billing bug fix", "description": "Investigate the billing calculation error"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "Created BILL-42: billing bug fix",
                },
                {"role": "assistant", "content": "Done! Created BILL-42."},
            ]
            mgr.save(conv)
            return mgr

    def test_search_finds_tool_call_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_tool_conversation(tmp)
                results = mgr.search("JIRA_CREATE_ISSUE")
                self.assertEqual(len(results), 1)

    def test_search_finds_tool_call_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_tool_conversation(tmp)
                results = mgr.search("billing calculation error")
                self.assertEqual(len(results), 1)

    def test_search_finds_tool_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = self._make_tool_conversation(tmp)
                results = mgr.search("BILL-42")
                self.assertEqual(len(results), 1)
                has_tool_match = any(
                    m["role"] == "tool" for r in results for m in r["matches"]
                )
                self.assertTrue(has_tool_match)

    def test_search_finds_anthropic_list_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = ConversationManager()
                conv = mgr.create(model="m", provider="p")
                conv.messages = [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "search for saunas"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Let me research Berkeley saunas!"},
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "spawn_agent",
                                "input": {"agent": "researcher", "task": "find saunas in Berkeley"},
                            },
                        ],
                    },
                ]
                mgr.save(conv)
                results = mgr.search("Berkeley saunas")
                self.assertEqual(len(results), 1)

    def test_search_finds_anthropic_tool_use_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": tmp}):
                mgr = ConversationManager()
                conv = mgr.create(model="m", provider="p")
                conv.messages = [
                    {"role": "system", "content": "s"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_2",
                                "name": "JIRA_GET_ISSUE",
                                "input": {"issue_key": "ENG-707"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_2",
                                "content": "ENG-707: regional admin session isolation bug",
                            },
                        ],
                    },
                ]
                mgr.save(conv)
                results = mgr.search("ENG-707")
                self.assertEqual(len(results), 1)
                self.assertGreaterEqual(len(results[0]["matches"]), 2)


class TestExtractSearchableText(unittest.TestCase):
    def test_string_content(self):
        text = _extract_searchable_text({"role": "user", "content": "hello world"})
        self.assertEqual(text, "hello world")

    def test_empty_content(self):
        text = _extract_searchable_text({"role": "assistant", "content": ""})
        self.assertEqual(text, "")

    def test_list_content_text_block(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "planning"}]}
        self.assertIn("planning", _extract_searchable_text(msg))

    def test_list_content_tool_use(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "local_shell", "input": {"command": "ls -la"}},
            ],
        }
        text = _extract_searchable_text(msg)
        self.assertIn("local_shell", text)
        self.assertIn("ls -la", text)

    def test_tool_calls_openai(self):
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "JIRA_CREATE_ISSUE", "arguments": '{"project": "CORE"}'},
                }
            ],
        }
        text = _extract_searchable_text(msg)
        self.assertIn("JIRA_CREATE_ISSUE", text)
        self.assertIn("CORE", text)

    def test_tool_result_in_list(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Created issue XYZ-99"},
            ],
        }
        self.assertIn("XYZ-99", _extract_searchable_text(msg))

    def test_role_tool_message(self):
        msg = {"role": "tool", "tool_call_id": "c1", "content": "exit code 0"}
        self.assertIn("exit code 0", _extract_searchable_text(msg))


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
