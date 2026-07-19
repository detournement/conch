"""Tests that /new and exit never block on the session-summary LLM call.

The summary is a side task: it runs on a background daemon thread so the
new conversation starts immediately even when the backend is slow or busy.
On exit the wait is bounded (a short join) rather than skipped entirely.
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


class TestSummarizeBoundedOnExit(unittest.TestCase):
    def test_slow_backend_never_blocks_exit_past_timeout(self):
        from conch.app import _summarize_and_save_bounded

        release = threading.Event()

        def stuck_raw_fn(config, msgs, tools):
            release.wait(30)
            return {"content": "- too late"}

        memory = FakeMemory()
        t0 = time.monotonic()
        _summarize_and_save_bounded(
            _messages(), {"provider": "ollama"}, stuck_raw_fn, memory, timeout=0.2
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0, "exit must not wait for a stuck summary call")
        self.assertEqual(memory.saved, [], "nothing saved within the bounded window")
        release.set()  # unblock the daemon thread before the test ends

    def test_fast_backend_summary_saved_before_exit(self):
        from conch.app import _summarize_and_save_bounded

        def fast_raw_fn(config, msgs, tools):
            return {"content": "- quick summary"}

        memory = FakeMemory()
        _summarize_and_save_bounded(
            _messages(), {"provider": "ollama"}, fast_raw_fn, memory, timeout=5
        )
        self.assertEqual(len(memory.saved), 1)
        self.assertIn("[Session summary]", memory.saved[0])

    def test_short_conversation_returns_immediately(self):
        from conch.app import _summarize_and_save_bounded

        calls = []

        def raw_fn(config, msgs, tools):
            calls.append(1)
            return {"content": "- summary"}

        memory = FakeMemory()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "only one turn"},
        ]
        _summarize_and_save_bounded(messages, {"provider": "ollama"}, raw_fn, memory)
        self.assertEqual(calls, [])
        self.assertEqual(memory.saved, [])


if __name__ == "__main__":
    unittest.main()
