"""Cross-mission memory consolidation gates (roadmap: mission judgment
and shared memory).

Proven here:

- After a mission session checkpoints, a consolidation pass distills
  journal-delta learnings into the shared memory tiers (conch/memory.py)
  tagged with mission id + topic — deduplicated, size-capped, and the
  pass itself is a post-checkpoint side task with a timeout that never
  blocks or breaks the session path (failure = logged skip; config off =
  fully skippable).
- Consolidation from mission A measurably changes mission B's rehydrated
  context: the clearly-labeled lessons block appears with A's exact
  learning, while B's total rehydration stays within its existing bound
  (MAX_CONTEXT_CHARS unchanged).
- The deterministic output scrubber rejects credential references, org
  UUIDs, and channel identities whole (never sanitizes), and the shared
  store is capped with oldest-mission-lesson eviction that never touches
  user memories.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.kernel import consolidate as consolidate_mod
from conch.kernel.engine import MAX_CONTEXT_CHARS, MissionEngine
from conch.kernel.store import MissionStore
from conch.memory import MemoryStore


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class ScriptedConsolidator:
    """Deterministic stand-in for the weak-model call."""

    def __init__(self, lessons=None, error="", delay=0.0, replies=None):
        self.lessons = lessons or []
        self.error = error
        self.delay = delay
        self.replies = list(replies or [])
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            return "", self.error
        if self.replies:
            return self.replies.pop(0), ""
        return json.dumps({"lessons": self.lessons}), ""


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        self.store = MissionStore(
            self.root / "kernel" / "kernel.db", clock=self.clock
        )
        self.addCleanup(lambda: self.store.close())

    def engine(self, consolidator=None, config=None):
        return MissionEngine(
            self.store,
            config or {"provider": "openai", "mission_reviews": "false"},
            holder="test-daemon",
            session_factory=lambda *a: (
                "session done", {"total_tokens": 9}
            ),
            consolidator=consolidator,
            kernel_dir=self.root / "kernel",
        )

    def make_mission(self, engine, goal, **overrides):
        spec = {
            "goal": goal,
            "budgets": {"sessions": 20, "tokens": 100000},
            "cadence_seconds": 86400,
        }
        spec.update(overrides)
        return engine.create_mission(spec, activate=True)


LESSON_OLLAMA = {
    "topic": "ollama concurrency",
    "lesson": (
        "The shared ollama box serializes model pulls; schedule only one"
        " pull at a time or requests time out."
    ),
}


class TestConsolidationWritesSharedMemory(MemoryCase):
    def test_lessons_land_tagged_deduped_and_capped(self):
        consolidator = ScriptedConsolidator(lessons=[
            LESSON_OLLAMA,
            {"topic": "ollama concurrency",
             "lesson": ("The shared ollama box serializes model pulls;"
                        " schedule only one pull at a time or requests"
                        " time out.")},  # exact duplicate within the pass
            {"topic": "x", "lesson": "y" * 900},  # over the per-lesson cap
        ])
        engine = self.engine(consolidator)
        mission_id = self.make_mission(engine, "watch the ollama endpoint")
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "waiting_timer")
        entries = MemoryStore().get_all()
        self.assertEqual(len(entries), 2)  # dup dropped, long one clipped
        tagged = [
            entry for entry in entries
            if entry["source"] == f"mission:{mission_id}"
        ]
        self.assertEqual(len(tagged), 2)
        self.assertIn("[ollama concurrency]", tagged[0]["content"])
        self.assertIn("serializes model pulls", tagged[0]["content"])
        cap = consolidate_mod.CONSOLIDATION_DEFAULTS["max_chars"]
        for entry in tagged:
            self.assertLessEqual(
                len(entry["content"]), cap + 60  # + topic tag
            )
        # The consolidator saw the journal delta, not the whole journal.
        self.assertIn("Journal delta", consolidator.calls[0])

    def test_same_lesson_not_resaved_across_sessions(self):
        consolidator = ScriptedConsolidator(lessons=[LESSON_OLLAMA])
        engine = self.engine(consolidator)
        mission_id = self.make_mission(engine, "watch the ollama endpoint")
        engine.run_session(mission_id)
        self.assertEqual(len(MemoryStore().get_all()), 1)
        # wake and run again; the model re-emits the same lesson
        self.clock.advance(86401)
        timer = self.store.find_timer(mission_id, "wake")
        self.store.claim_due_timers("test-daemon")
        self.store.fire_timer(
            timer["timer_id"], timer["generation"], holder="test-daemon"
        )
        engine.run_session(mission_id)
        self.assertEqual(len(consolidator.calls), 2)
        self.assertEqual(len(MemoryStore().get_all()), 1)  # no re-save

    def test_config_off_never_calls_the_model(self):
        consolidator = ScriptedConsolidator(lessons=[LESSON_OLLAMA])
        engine = self.engine(consolidator, config={
            "provider": "openai", "mission_reviews": "false",
            "mission_consolidation": "false",
        })
        mission_id = self.make_mission(engine, "quiet mission")
        engine.run_session(mission_id)
        self.assertEqual(consolidator.calls, [])
        self.assertEqual(MemoryStore().get_all(), [])

    def test_scheduled_prompts_are_not_consolidated(self):
        consolidator = ScriptedConsolidator(lessons=[LESSON_OLLAMA])
        engine = self.engine(consolidator)
        mission_id = engine.create_mission({
            "goal": "run: check disk", "kind": "scheduled_prompt",
            "prompt": "check disk", "cadence_seconds": 3600, "budgets": {},
        })
        self.clock.advance(3601)
        timer = self.store.find_timer(mission_id, "wake")
        self.store.claim_due_timers("test-daemon")
        self.store.fire_timer(
            timer["timer_id"], timer["generation"], holder="test-daemon"
        )
        engine.run_session(mission_id)
        self.assertEqual(consolidator.calls, [])

    def test_failure_is_a_logged_skip_never_a_session_error(self):
        logs = []
        engine = MissionEngine(
            self.store,
            {"provider": "openai", "mission_reviews": "false"},
            holder="test-daemon",
            session_factory=lambda *a: ("done", {"total_tokens": 3}),
            consolidator=ScriptedConsolidator(error="weak model down"),
            kernel_dir=self.root / "kernel",
            log=logs.append,
        )
        mission_id = self.make_mission(engine, "resilient mission")
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "waiting_timer")
        self.assertEqual(result["error"], "")
        self.assertTrue(
            any("consolidation" in line and "skipped" in line
                for line in logs),
            logs,
        )
        self.assertEqual(MemoryStore().get_all(), [])

    def test_timeout_never_blocks_and_suppresses_the_late_write(self):
        consolidator = ScriptedConsolidator(
            lessons=[LESSON_OLLAMA], delay=0.4
        )
        engine = self.engine(consolidator, config={
            "provider": "openai", "mission_reviews": "false",
            "mission_consolidation_timeout": "0.05",
        })
        mission_id = self.make_mission(engine, "slow model mission")
        started = time.monotonic()
        result = engine.run_session(mission_id)
        elapsed = time.monotonic() - started
        self.assertEqual(result["outcome"], "waiting_timer")
        self.assertLess(elapsed, 0.35)  # returned before the model did
        time.sleep(0.6)  # let the abandoned worker finish
        self.assertEqual(MemoryStore().get_all(), [])

    def test_store_cap_evicts_oldest_mission_lessons_only(self):
        memory = MemoryStore()
        memory.add("user note: prefers rsync over scp", source="user")
        for index in range(4):
            memory.add(f"[t{index}] mission lesson {index}",
                       source=f"mission:msn-{index}")
        worker = consolidate_mod._Consolidation(
            lambda text: (json.dumps({"lessons": [
                {"topic": "new", "lesson": "a fresh lesson worth keeping"
                                           " around"},
            ]}), ""),
            "delta", "msn-new",
            {**consolidate_mod.CONSOLIDATION_DEFAULTS, "store_cap": 3},
        )
        worker.run()
        self.assertEqual(worker.result["status"], "ok")
        entries = MemoryStore().get_all()
        sources = [entry["source"] for entry in entries]
        self.assertIn("user", sources)  # user memories never evicted
        mission_entries = [
            entry for entry in entries
            if str(entry["source"]).startswith("mission:")
        ]
        self.assertEqual(len(mission_entries), 3)
        self.assertNotIn(
            "mission:msn-0",
            [entry["source"] for entry in mission_entries],
        )
        self.assertIn(
            "mission:msn-new",
            [entry["source"] for entry in mission_entries],
        )


class TestCrossMissionRehydration(MemoryCase):
    """THE gate: consolidation from mission A measurably changes mission
    B's rehydrated context, within B's existing size bound."""

    def test_mission_a_lesson_reaches_mission_b_context(self):
        consolidator = ScriptedConsolidator(lessons=[LESSON_OLLAMA])
        engine = self.engine(consolidator)
        mission_a = self.make_mission(
            engine, "watch the shared ollama endpoint for degradation"
        )
        engine.run_session(mission_a)
        expected = (
            "[ollama concurrency] The shared ollama box serializes model"
            " pulls; schedule only one pull at a time or requests time"
            " out."
        )
        entries = MemoryStore().get_all()
        self.assertEqual(entries[0]["content"], expected)

        mission_b = self.make_mission(
            engine, "benchmark nightly model pulls on the shared ollama box"
        )
        context = engine.build_context(self.store.get_mission(mission_b))
        self.assertIn(consolidate_mod.LESSONS_LABEL, context)
        self.assertIn(expected, context)          # exact retrieval
        self.assertIn(f"mission:{mission_a}", context)  # provenance tag
        self.assertLessEqual(len(context), MAX_CONTEXT_CHARS)

        # Control: without the lessons block the context would not know
        # about serialized pulls.
        engine_off = self.engine(config={
            "provider": "openai", "mission_reviews": "false",
            "mission_consolidation": "false",
        })
        control_context = engine_off.build_context(
            self.store.get_mission(mission_b)
        )
        self.assertNotIn("serializes model pulls", control_context)

    def test_own_lessons_are_excluded_from_own_context(self):
        consolidator = ScriptedConsolidator(lessons=[LESSON_OLLAMA])
        engine = self.engine(consolidator)
        mission_a = self.make_mission(
            engine, "watch the shared ollama endpoint for degradation"
        )
        engine.run_session(mission_a)
        context = engine.build_context(self.store.get_mission(mission_a))
        self.assertNotIn(consolidate_mod.LESSONS_LABEL, context)

    def test_k_and_char_caps_bound_the_block(self):
        memory = MemoryStore()
        for index in range(6):
            memory.add(
                f"[digest topic {index}] repo digest lesson number {index}"
                f" about summarizing commits",
                source=f"mission:msn-old-{index}",
            )
        engine = self.engine(config={
            "provider": "openai", "mission_reviews": "false",
            "mission_lessons_k": "2",
            "mission_lessons_max_chars": "200",
        })
        mission_b = self.make_mission(
            engine, "produce the repo digest summarizing commits"
        )
        context = engine.build_context(self.store.get_mission(mission_b))
        block_lines = [
            line for line in context.splitlines()
            if line.strip().startswith("- [digest topic")
        ]
        self.assertLessEqual(len(block_lines), 2)
        start = context.find(consolidate_mod.LESSONS_LABEL)
        self.assertGreaterEqual(start, 0)
        block = context[start:]
        block = block.split("\n\n")[0]
        self.assertLessEqual(len(block), 200 + 40)

    def test_rehydration_bound_holds_under_huge_journal_and_lessons(self):
        memory = MemoryStore()
        for index in range(30):
            memory.add(
                f"[bulk {index}] shared lesson {index} about digests and"
                " commits " + "detail " * 20,
                source=f"mission:msn-bulk-{index}",
            )
        engine = self.engine()
        mission_b = self.make_mission(
            engine, "produce the repo digest summarizing commits"
        )
        for index in range(1500):
            self.store.record_note(
                mission_b, f"observation {index}: " + "x" * 200
            )
        context = engine.build_context(self.store.get_mission(mission_b))
        self.assertLessEqual(len(context), MAX_CONTEXT_CHARS)


class TestScrubber(unittest.TestCase):
    def test_rejections(self):
        cases = {
            "contains a UUID": (
                "[orgs] provision under org"
                " 123e4567-e89b-12d3-a456-426614174000 next time"
            ),
            "long token": (
                "[keys] reuse CANARY-9f2c1e-hunter2-do-not-log for the"
                " sandbox"
            ),
            "credential material": (
                "[auth] the api key lives in the org bundle"
            ),
            "channel identifier": (
                "[slack] ping U0AAAAAA7 when the digest lands"
            ),
            "email": "[contact] mail results to ops@example.com nightly",
            "phone-like": "[sms] text +15559998888 on failure",
            "long digit run": "[account] use account 12345678901",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.assertTrue(
                    consolidate_mod.lesson_rejection(text), text
                )

    def test_clean_operational_lessons_pass(self):
        clean = [
            "[ollama concurrency] The shared ollama box serializes model"
            " pulls; schedule only one pull at a time.",
            "[git digests] git log --since=24h is enough for the daily"
            " digest; a full clone is wasted work.",
            "[retries] eBay sandbox drafts fail transiently around"
            " midnight; retry once after a minute.",
            "[budgets] Keep session token budgets under half the window"
            " for CONSOLIDATION to stay reliable.",
        ]
        for text in clean:
            with self.subTest(text=text[:30]):
                self.assertEqual(
                    consolidate_mod.lesson_rejection(text), ""
                )

    def test_duplicate_detection(self):
        existing = [
            "[ollama concurrency] The shared ollama box serializes model"
            " pulls; schedule only one pull at a time."
        ]
        self.assertTrue(consolidate_mod.is_duplicate(
            "[ollama concurrency] the shared ollama box serializes model"
            " pulls — schedule only one pull at a time!", existing,
        ))
        self.assertFalse(consolidate_mod.is_duplicate(
            "[printing] label PDFs must be rendered at 300dpi or the"
            " barcode is rejected.", existing,
        ))


class TestRankEntries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_source_filtering_and_ranking(self):
        memory = MemoryStore()
        memory.add("user prefers vim keybindings", source="user")
        memory.add("[digests] daily digests need git log", source="mission:a")
        memory.add("[labels] print labels at 300dpi", source="mission:b")
        ranked = memory.rank_entries(
            "produce the daily digest with git log", limit=5,
            source_prefix="mission:",
        )
        self.assertEqual(ranked[0]["source"], "mission:a")
        self.assertTrue(
            all(entry["source"].startswith("mission:") for entry in ranked)
        )
        excluded = memory.rank_entries(
            "produce the daily digest with git log", limit=5,
            source_prefix="mission:", exclude_source="mission:a",
        )
        self.assertNotIn(
            "mission:a", [entry["source"] for entry in excluded]
        )


if __name__ == "__main__":
    unittest.main()
