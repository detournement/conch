"""Session-summary noise (review finding F13): a `[Session summary]`
memory is written only for sessions with substance — at least
`session_summary_min_turns` user turns, or any tool use — and a summary
near-identical to one already stored is not written again."""

import tempfile
import unittest
from unittest import mock

from conch.app import (
    SESSION_SUMMARY_MIN_TURNS_DEFAULT,
    _summarize_and_save,
    _summarize_and_save_async,
    session_summary_min_turns,
    session_worth_summarizing,
)
from conch.memory import MemoryStore


def _turns(n, tool_use=False):
    messages = [{"role": "system", "content": "sys"}]
    for index in range(n):
        messages.append({"role": "user", "content": f"question {index}"})
        if tool_use and index == 0:
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "local_shell",
                                             "arguments": "{\"command\": \"ls\"}"}}],
            })
            messages.append({"role": "tool", "tool_call_id": "c1", "content": "a.txt"})
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


class TestThresholdSetting(unittest.TestCase):
    def test_default_is_three_turns(self):
        self.assertEqual(SESSION_SUMMARY_MIN_TURNS_DEFAULT, 3)
        self.assertEqual(session_summary_min_turns({}), 3)
        from conch.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG["session_summary_min_turns"], "3")

    def test_setting_is_honoured_and_zero_or_off_disables(self):
        self.assertEqual(session_summary_min_turns({"session_summary_min_turns": "5"}), 5)
        self.assertIsNone(session_summary_min_turns({"session_summary_min_turns": "0"}))
        self.assertIsNone(session_summary_min_turns({"session_summary_min_turns": "off"}))
        self.assertEqual(session_summary_min_turns({"session_summary_min_turns": "junk"}), 3)

    def test_env_var_maps_to_the_setting(self):
        from conch.config import ENV_CONFIG_KEYS
        self.assertEqual(
            ENV_CONFIG_KEYS["CONCH_SESSION_SUMMARY_MIN_TURNS"], "session_summary_min_turns"
        )


class TestWorthSummarizing(unittest.TestCase):
    def test_trivial_sessions_are_not_summarized(self):
        for n in (1, 2):
            self.assertFalse(session_worth_summarizing(_turns(n), {}), n)
        self.assertFalse(session_worth_summarizing([], {}))

    def test_enough_turns_qualifies(self):
        self.assertTrue(session_worth_summarizing(_turns(3), {}))
        self.assertFalse(session_worth_summarizing(_turns(3), {"session_summary_min_turns": "4"}))
        self.assertTrue(session_worth_summarizing(_turns(2), {"session_summary_min_turns": "2"}))

    def test_tool_use_qualifies_even_when_short(self):
        self.assertTrue(session_worth_summarizing(_turns(1, tool_use=True), {}))
        anthropic_shape = [
            {"role": "user", "content": "what is here"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "local_shell", "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
            {"role": "assistant", "content": "x is here"},
        ]
        self.assertTrue(session_worth_summarizing(anthropic_shape, {}))

    def test_disabled_never_summarizes(self):
        self.assertFalse(
            session_worth_summarizing(_turns(10, tool_use=True), {"session_summary_min_turns": "0"})
        )


class TestSummaryWrites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"XDG_STATE_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_trivial_session_writes_nothing_and_makes_no_llm_call(self):
        calls = []

        def raw_fn(config, msgs, tools):
            calls.append(1)
            return {"content": "- said hi"}

        memory = MemoryStore()
        _summarize_and_save(_turns(2), {"provider": "ollama"}, raw_fn, memory)
        self.assertEqual(calls, [])
        self.assertEqual(MemoryStore().get_all(), [])
        self.assertIsNone(_summarize_and_save_async(_turns(2), {"provider": "ollama"}, raw_fn, memory))

    def test_near_identical_summary_is_not_written_twice(self):
        def raw_fn(config, msgs, tools):
            return {"content": "- Sorted the Downloads folder by date.\n- Removed empty dirs."}

        def raw_fn_variant(config, msgs, tools):
            return {"content": "• sorted the downloads folder by date\n• removed empty dirs"}

        memory = MemoryStore()
        _summarize_and_save(_turns(3), {"provider": "ollama"}, raw_fn, memory)
        _summarize_and_save(_turns(3), {"provider": "ollama"}, raw_fn, memory)
        _summarize_and_save(_turns(3), {"provider": "ollama"}, raw_fn_variant, memory)
        entries = MemoryStore().get_all()
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["source"], "summary")

    def test_different_summary_is_written(self):
        replies = iter([
            "- Sorted the Downloads folder by date.",
            "- Set up the nginx reverse proxy and renewed the certificate.",
        ])

        def raw_fn(config, msgs, tools):
            return {"content": next(replies)}

        memory = MemoryStore()
        _summarize_and_save(_turns(3), {"provider": "ollama"}, raw_fn, memory)
        _summarize_and_save(_turns(3), {"provider": "ollama"}, raw_fn, memory)
        self.assertEqual(len(MemoryStore().get_all()), 2)

    def test_dedupe_only_looks_at_summaries(self):
        memory = MemoryStore()
        memory.add("[Session summary] Sorted the Downloads folder by date.", source="user")

        def raw_fn(config, msgs, tools):
            return {"content": "Sorted the Downloads folder by date."}

        _summarize_and_save(_turns(3), {"provider": "ollama"}, raw_fn, memory)
        self.assertEqual(len(MemoryStore().get_all()), 2,
                         "a user-written memory never suppresses a summary")


class TestNearDuplicate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"XDG_STATE_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_similarity_threshold(self):
        memory = MemoryStore()
        memory.add("[Session summary] - Installed ripgrep and fd via brew.", source="summary")
        self.assertIsNotNone(
            memory.near_duplicate("[Session summary] • installed ripgrep and fd via brew", source="summary")
        )
        self.assertIsNone(
            memory.near_duplicate("[Session summary] - Rotated the API keys for staging.", source="summary")
        )
        self.assertIsNone(memory.near_duplicate("", source="summary"))


if __name__ == "__main__":
    unittest.main()
