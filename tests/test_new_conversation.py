"""Tests that /new never blocks on the session-summary LLM call.

The summary is a side task: it runs on a background daemon thread so the
new conversation starts immediately even when the backend is slow or busy.
"""

import threading
import time
import unittest


class FakeMemory:
    def __init__(self):
        self.saved = []

    def add(self, content, source=""):
        self.saved.append(content)


def _messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "two"},
    ]


class TestSummarizeAsync(unittest.TestCase):
    def test_returns_before_llm_call_completes(self):
        from conch.app import _summarize_and_save_async

        started = threading.Event()
        release = threading.Event()

        def slow_raw_fn(config, msgs, tools):
            started.set()
            release.wait(5)
            return {"content": "- summary"}

        memory = FakeMemory()
        t0 = time.monotonic()
        thread = _summarize_and_save_async(
            _messages(), {"provider": "ollama"}, slow_raw_fn, memory
        )
        elapsed = time.monotonic() - t0

        try:
            self.assertIsNotNone(thread)
            self.assertLess(elapsed, 0.5, "/new must not wait for the summary LLM call")
            self.assertTrue(started.wait(2), "summary must still run in the background")
            self.assertEqual(memory.saved, [], "summary not saved before LLM finishes")
        finally:
            release.set()
        thread.join(5)
        self.assertEqual(len(memory.saved), 1)
        self.assertIn("[Session summary]", memory.saved[0])

    def test_short_conversations_spawn_no_thread(self):
        from conch.app import _summarize_and_save_async

        calls = []

        def raw_fn(config, msgs, tools):
            calls.append(1)
            return {"content": "- summary"}

        memory = FakeMemory()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "only one turn"},
        ]
        thread = _summarize_and_save_async(messages, {"provider": "ollama"}, raw_fn, memory)
        self.assertIsNone(thread)
        self.assertEqual(calls, [])
        self.assertEqual(memory.saved, [])

    def test_snapshot_isolated_from_new_conversation_reset(self):
        """The background summary must see the old transcript even though
        /new immediately rebinds `messages` to a fresh list."""
        from conch.app import _summarize_and_save_async

        release = threading.Event()
        seen = {}

        def raw_fn(config, msgs, tools):
            release.wait(5)
            # msgs[1:-1] are the transcript turns (system replaced, prompt appended)
            seen["turns"] = [m["content"] for m in msgs[1:-1]]
            return {"content": "- summary"}

        memory = FakeMemory()
        messages = _messages()
        thread = _summarize_and_save_async(messages, {"provider": "ollama"}, raw_fn, memory)
        messages.clear()  # what happens conceptually on /new
        release.set()
        thread.join(5)
        self.assertEqual(seen["turns"], ["one", "reply", "two"])


if __name__ == "__main__":
    unittest.main()
