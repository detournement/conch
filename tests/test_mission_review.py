"""Mission judgment gates (roadmap: mission judgment and shared memory).

Proven here:

- Deterministic stall detection from kernel events only — no material
  state change across N sessions (material = plan/task/action/artifact/
  approval/binding events) and repeated failures of the same step —
  computed BEFORE any model call and journaled with the verdict.
- The stall fixture (N quiet sessions + a repeatedly failing task)
  triggers a review whose re-plan lands as a new numbered plan version
  through the existing plans machinery, journaled as an explicit
  ``plan_revised`` event with rationale; replay == live.
- A stalled mission can never "continue": the engine escalates through
  the existing outbox notify path (never silent), including when the
  review model is unusable.
- Reviews respect budgets: an exhausted ``reviews`` line skips with a
  journaled ``review_skipped`` event and never crashes or fails the
  mission; the skip advances the cadence marker (no skip spam).
- Cadence: every N work sessions or every_seconds, whichever first;
  scheduled prompts are never reviewed; reviews never increment ``runs``
  or write checkpoints.
- STOP/pause gates suppress reviews exactly like work sessions.
- Crash safety: a review that outlives its lease is refused and repaired
  by the normal reconcile path.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.kernel import review as review_mod
from conch.kernel.engine import MissionEngine
from conch.kernel.model import KernelError, MissionState, normalize_spec
from conch.kernel.store import MissionStore


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class ScriptedFactory:
    """Work-session runner: optionally drives mission_control per call."""

    def __init__(self, reply="worked", scripts=None, hook=None):
        self.reply = reply
        self.scripts = list(scripts or [])
        self.hook = hook
        self.calls = 0

    def __call__(self, mission, messages, control, caps):
        self.calls += 1
        if self.scripts:
            for op in self.scripts.pop(0):
                control.call_tool("mission_control", op)
        if self.hook is not None:
            self.hook(mission)
        return self.reply, {"total_tokens": 100}


class ScriptedReviewRunner:
    """Review runner: records what the critic saw, returns scripted JSON."""

    def __init__(self, verdicts=None, error="", exception=None):
        self.verdicts = list(verdicts or [])
        self.error = error
        self.exception = exception
        self.calls = []

    def __call__(self, mission, context, signals):
        self.calls.append({
            "mission": mission, "context": context,
            "signals": dict(signals),
        })
        if self.exception is not None:
            raise self.exception
        if self.error:
            return "", {}, self.error
        verdict = self.verdicts.pop(0) if self.verdicts else {
            "criteria": [], "action": "continue", "rationale": "fine",
        }
        return json.dumps(verdict), {"total_tokens": 40, "model": "w"}, ""


BASE_SPEC = {
    "goal": "ship the weekly digest",
    "success_criteria": ["digest exists", "digest was delivered"],
    "budgets": {"tokens": 500000, "sessions": 50},
    "cadence_seconds": 86400,
    # Session-count cadence only; time-based reviews get their own test.
    "review": {"every_sessions": 3, "every_seconds": 0,
               "stall_sessions": 3},
}


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kernel_dir = Path(self._tmp.name) / "kernel"
        self.clock = FakeClock()
        self.store = MissionStore(
            self.kernel_dir / "kernel.db", clock=self.clock
        )
        self.addCleanup(lambda: self.store.close())

    def engine(self, factory=None, review_runner=None, config=None):
        return MissionEngine(
            self.store, config or {
                "provider": "openai", "model": "gpt-4o",
                "mission_consolidation": "false",
            },
            holder="test-daemon", session_factory=factory,
            review_runner=review_runner, kernel_dir=self.kernel_dir,
        )

    def make_mission(self, engine, **overrides):
        spec = dict(BASE_SPEC)
        spec.update(overrides)
        return engine.create_mission(spec, activate=True)

    def wake(self, mission_id):
        """Advance past the cadence and fire the wake timer."""
        self.clock.advance(86401)
        timer = self.store.find_timer(mission_id, "wake")
        self.store.claim_due_timers("test-daemon")
        self.store.fire_timer(
            timer["timer_id"], timer["generation"], holder="test-daemon"
        )

    def run_next(self, engine, mission_id):
        """Wake a timer-parked mission (as the daemon's timer fire would)
        and run its next session slot."""
        mission = self.store.get_mission(mission_id)
        if mission["status"] == MissionState.WAITING_TIMER:
            self.wake(mission_id)
        return engine.run_session(mission_id)

    def run_work_sessions(self, engine, mission_id, count):
        return [
            self.run_next(engine, mission_id) for _ in range(count)
        ]

    def event_kinds(self, mission_id):
        return [
            event["kind"]
            for event in self.store.events_since(mission_id, 0, (), 500)
        ]


class TestDeterministicStallDetection(ReviewCase):
    """The detector is a pure function of kernel events — no model."""

    def test_quiet_sessions_are_a_material_change_stall(self):
        factory = ScriptedFactory()
        engine = self.engine(factory)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        policy = review_mod.resolve_policy(
            self.store.get_mission(mission_id)["spec"], {}
        )
        signals = review_mod.stall_signals(self.store, mission_id, policy)
        self.assertEqual(signals["window_sessions"], 3)
        self.assertEqual(signals["material_total"], 0)
        self.assertTrue(signals["no_material_change"])
        self.assertFalse(signals["repeated_failure"])
        self.assertTrue(signals["stalled"])

    def test_material_events_clear_the_stall(self):
        factory = ScriptedFactory(scripts=[
            [{"op": "update_plan", "steps": ["a", "b"]}],
            [{"op": "add_task", "text": "investigate"}],
            [{"op": "note", "text": "still looking"}],  # notes not material
        ])
        engine = self.engine(factory)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        policy = review_mod.resolve_policy(
            self.store.get_mission(mission_id)["spec"], {}
        )
        signals = review_mod.stall_signals(self.store, mission_id, policy)
        self.assertGreaterEqual(signals["material_total"], 2)
        self.assertFalse(signals["stalled"])

    def test_repeated_failures_of_the_same_step_stall(self):
        engine = self.engine(ScriptedFactory())
        mission_id = self.make_mission(engine)
        task_id = self.store.create_task(mission_id, "flaky fetch")
        # A review marker isolates the window from the (material)
        # task_created event, then the same step fails twice.
        self.store.record_review_skip(mission_id, "baseline marker")
        for _ in range(2):
            attempt = self.store.start_attempt(task_id)
            self.store.finish_attempt(
                attempt, "failed", failure_class="transient",
                detail="fetch timed out",
            )
        self.run_work_sessions(engine, mission_id, 2)
        policy = review_mod.resolve_policy(
            self.store.get_mission(mission_id)["spec"], {}
        )
        signals = review_mod.stall_signals(self.store, mission_id, policy)
        self.assertTrue(signals["repeated_failure"])
        self.assertIn(task_id, signals["repeat_detail"])
        self.assertTrue(signals["stalled"])

    def test_repeated_identical_session_errors_stall(self):
        factory = ScriptedFactory()
        engine = self.engine(factory)
        mission_id = self.make_mission(engine)

        def failing(mission, messages, control, caps):
            raise RuntimeError("provider down")

        engine._session_factory = failing
        self.run_work_sessions(engine, mission_id, 2)
        policy = review_mod.resolve_policy(
            self.store.get_mission(mission_id)["spec"], {}
        )
        signals = review_mod.stall_signals(self.store, mission_id, policy)
        self.assertTrue(signals["repeated_failure"])
        self.assertIn("same error", signals["repeat_detail"])


class TestStallFixtureProducesPlanRevision(ReviewCase):
    """THE gate: the stall fixture (N quiet sessions + a repeatedly
    failing step) triggers a review whose re-plan is a journaled numbered
    plan version — deterministically detectable in kernel events."""

    def test_stall_review_replan_journaled_and_replayable(self):
        task_holder = {}

        def fail_step_hook(mission):
            attempt = self.store.start_attempt(task_holder["task_id"])
            self.store.finish_attempt(
                attempt, "failed", failure_class="transient",
                detail="step 1 keeps timing out",
            )

        factory = ScriptedFactory(scripts=[
            [{"op": "update_plan", "steps": ["fetch the feed",
                                             "write the digest"]}],
        ])
        runner = ScriptedReviewRunner(verdicts=[
            {   # review 1: healthy
                "criteria": [
                    {"criterion": "digest exists", "verdict": "on-track",
                     "evidence": "plan recorded, work started"},
                ],
                "action": "continue", "rationale": "early but moving",
            },
            {   # review 2: the fixture is stalled — re-plan
                "criteria": [
                    {"criterion": "digest exists", "verdict": "stalled",
                     "evidence": "same fetch failure in every session"},
                    {"criterion": "digest was delivered",
                     "verdict": "at-risk",
                     "evidence": "nothing produced to deliver"},
                ],
                "action": "re-plan",
                "rationale": "the feed endpoint is dead; switch source",
                "plan": ["pull commits with git log directly",
                         "write the digest from the log",
                         "deliver via the channel"],
            },
        ])
        engine = self.engine(factory, review_runner=runner)
        mission_id = self.make_mission(engine)
        task_holder["task_id"] = self.store.create_task(
            mission_id, "fetch the feed"
        )

        # Three work sessions (the first records plan v1) → review 1.
        self.run_work_sessions(engine, mission_id, 3)
        first = self.run_next(engine, mission_id)
        self.assertEqual(first["outcome"], "review_continue")
        self.assertFalse(runner.calls[0]["signals"]["stalled"])

        # The stall fixture: three quiet sessions, the same step failing
        # every time, no material deltas.
        engine._session_factory = ScriptedFactory(hook=fail_step_hook)
        self.run_work_sessions(engine, mission_id, 3)
        second = self.run_next(engine, mission_id)
        self.assertEqual(second["outcome"], "review_replan")

        # Deterministic detection happened BEFORE the model saw anything:
        # the runner received the computed signals and a STALLED context.
        signals = runner.calls[1]["signals"]
        self.assertTrue(signals["stalled"])
        self.assertTrue(signals["no_material_change"])
        self.assertTrue(signals["repeated_failure"])
        self.assertEqual(signals["window_sessions"], 3)
        self.assertEqual(signals["material_total"], 0)
        self.assertIn("STALLED", runner.calls[1]["context"])

        # The re-plan is a new numbered plan version through the existing
        # plans machinery, journaled as an explicit plan_revised event.
        plan = self.store.latest_plan(mission_id)
        self.assertEqual(plan["version"], 2)
        self.assertEqual(
            plan["content"]["steps"][0], "pull commits with git log directly"
        )
        revision = self.store.last_event(mission_id, ("plan_revised",))
        self.assertIsNotNone(revision)
        self.assertEqual(revision["data"]["from_version"], 1)
        self.assertEqual(revision["data"]["to_version"], 2)
        self.assertIn("feed endpoint is dead", revision["data"]["rationale"])
        review_event = self.store.last_event(
            mission_id, ("review_recorded",)
        )
        self.assertEqual(review_event["data"]["action"], "re-plan")
        self.assertTrue(
            review_event["data"]["content"]["stall"]["stalled"]
        )

        # The verdict is queryable for /mission show.
        latest = self.store.latest_review(mission_id)
        self.assertEqual(latest["action"], "re-plan")
        self.assertEqual(len(latest["content"]["criteria"]), 2)

        # Replay == live with the new events and projection.
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

        # The next session slot runs WORK against the revised plan.
        recorder = {}

        def recording_factory(mission, messages, control, caps):
            recorder["context"] = messages[1]["content"]
            return "onward", {"total_tokens": 10}

        engine._session_factory = recording_factory
        third = self.run_next(engine, mission_id)
        self.assertEqual(third["outcome"], MissionState.WAITING_TIMER)
        self.assertIn("Current plan (v2)", recorder["context"])


class TestNeverSilentOnStall(ReviewCase):
    def test_stalled_continue_is_coerced_to_escalation(self):
        runner = ScriptedReviewRunner(verdicts=[
            {"criteria": [], "action": "continue",
             "rationale": "looks fine to me"},
        ])
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(engine, channel="slack")
        self.run_work_sessions(engine, mission_id, 3)
        result = self.run_next(engine, mission_id)
        self.assertEqual(result["outcome"], "review_escalate")
        review = self.store.latest_review(mission_id)
        self.assertEqual(review["action"], "escalate")
        self.assertIn("deterministic stall", review["content"]["rationale"])
        outbox = self.store.list_outbox(mission_id=mission_id)
        payloads = [row["payload"] for row in outbox]
        self.assertTrue(
            any("review escalation" in payload for payload in payloads),
            payloads,
        )

    def test_unusable_model_on_stalled_mission_escalates(self):
        runner = ScriptedReviewRunner(error="connection refused")
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        result = self.run_next(engine, mission_id)
        self.assertEqual(result["outcome"], "review_escalate")
        review = self.store.latest_review(mission_id)
        self.assertIn("review model was unusable",
                      review["content"]["rationale"])
        self.assertTrue(
            any("review escalation" in row["payload"]
                for row in self.store.list_outbox(mission_id=mission_id))
        )

    def test_unusable_model_without_stall_is_a_journaled_skip(self):
        runner = ScriptedReviewRunner(error="connection refused")
        factory = ScriptedFactory(scripts=[
            [{"op": "add_task", "text": "material progress"}], [], [],
        ])
        engine = self.engine(factory, review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        result = self.run_next(engine, mission_id)
        self.assertEqual(result["outcome"], "review_skipped")
        skip = self.store.last_event(mission_id, ("review_skipped",))
        self.assertIn("connection refused", skip["data"]["reason"])
        self.assertIsNone(self.store.latest_review(mission_id))
        # The mission is unharmed and runnable.
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)


class TestReviewBudgets(ReviewCase):
    def test_exhausted_review_budget_skips_never_crashes(self):
        runner = ScriptedReviewRunner()
        factory = ScriptedFactory(scripts=[
            [{"op": "add_task", "text": "step one"}], [], [],
            [{"op": "add_task", "text": "step two"}], [], [],
        ])
        engine = self.engine(factory, review_runner=runner)
        mission_id = self.make_mission(
            engine, budgets={"reviews": 1, "sessions": 50, "tokens": 500000},
        )
        self.run_work_sessions(engine, mission_id, 3)
        first = self.run_next(engine, mission_id)
        self.assertEqual(first["outcome"], "review_continue")
        mission = self.store.get_mission(mission_id)
        status = self.store.budget_status(mission["root_scope_id"])
        self.assertEqual(status["reviews"]["committed"], 1)
        self.assertEqual(status["reviews"]["available"], 0)

        # Second cadence: the review line is spent → journaled skip.
        self.run_work_sessions(engine, mission_id, 3)
        second = self.run_next(engine, mission_id)
        self.assertEqual(second["outcome"], "review_skipped")
        self.assertIn("budget", second["error"])
        skip = self.store.last_event(mission_id, ("review_skipped",))
        self.assertIn("review budget exhausted", skip["data"]["reason"])
        # Only one model call ever happened; the mission is not failed.
        self.assertEqual(len(runner.calls), 1)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)

        # The skip advanced the cadence marker: the very next slot runs
        # WORK, not another skip (no skip spam).
        third = self.run_next(engine, mission_id)
        self.assertEqual(third["outcome"], MissionState.WAITING_TIMER)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_review_token_usage_commits_within_reservation(self):
        runner = ScriptedReviewRunner()
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        mission = self.store.get_mission(mission_id)
        before = self.store.budget_status(
            mission["root_scope_id"]
        )["tokens"]["committed"]
        self.run_next(engine, mission_id)
        after = self.store.budget_status(
            mission["root_scope_id"]
        )["tokens"]["committed"]
        self.assertEqual(after - before, 40)  # the runner's usage
        self.assertEqual(
            self.store.budget_status(
                mission["root_scope_id"]
            )["tokens"]["reserved"],
            0,
        )


class TestReviewCadenceAndGates(ReviewCase):
    def test_time_based_cadence_reviews_daily(self):
        runner = ScriptedReviewRunner()
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(
            engine,
            review={"every_sessions": 99, "every_seconds": 3600,
                    "stall_sessions": 3},
        )
        engine.run_session(mission_id)  # one work session
        self.clock.advance(3700)
        woken = engine.wake_mission(mission_id, reason="test wake")
        self.assertTrue(woken)
        result = self.run_next(engine, mission_id)
        self.assertEqual(result["outcome"], "review_continue")

    def test_no_review_without_new_work_sessions(self):
        runner = ScriptedReviewRunner()
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(
            engine,
            review={"every_sessions": 1, "every_seconds": 0,
                    "stall_sessions": 3},
        )
        engine.run_session(mission_id)           # work
        review = self.run_next(engine, mission_id)
        self.assertEqual(review["outcome"], "review_continue")
        # No work since the review → the next slot is work, not a review.
        again = self.run_next(engine, mission_id)
        self.assertEqual(again["outcome"], MissionState.WAITING_TIMER)

    def test_scheduled_prompts_are_never_reviewed(self):
        policy = review_mod.resolve_policy(
            normalize_spec({
                "goal": "run: x", "kind": "scheduled_prompt", "prompt": "x",
                "review": {"enabled": True},
            }),
            {},
        )
        self.assertFalse(policy["enabled"])

    def test_config_defaults_and_spec_overrides(self):
        policy = review_mod.resolve_policy(
            normalize_spec({"goal": "g"}),
            {"mission_review_every_sessions": "7"},
        )
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["every_sessions"], 7)
        self.assertEqual(policy["every_seconds"], 86400)
        policy = review_mod.resolve_policy(
            normalize_spec({"goal": "g", "review": {"enabled": False}}), {}
        )
        self.assertFalse(policy["enabled"])
        policy = review_mod.resolve_policy(
            normalize_spec({"goal": "g"}), {"mission_reviews": "false"}
        )
        self.assertFalse(policy["enabled"])

    def test_unknown_review_spec_fields_fail_closed(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "g", "review": {"cadence": 5}})
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "g", "review": {"every_sessions": 0}})

    def test_stop_file_and_flag_suppress_reviews(self):
        runner = ScriptedReviewRunner()
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        self.wake(mission_id)  # ready, and a review is due
        engine.stop_file().parent.mkdir(parents=True, exist_ok=True)
        engine.stop_file().write_text("halt\n")
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "skipped")
        engine.stop_file().unlink()
        self.store.set_stop(mission_id, True)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "paused")
        self.assertEqual(len(runner.calls), 0)
        self.assertNotIn("review_recorded", self.event_kinds(mission_id))

    def test_paused_and_parked_missions_never_review(self):
        runner = ScriptedReviewRunner()
        engine = self.engine(ScriptedFactory(), review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        engine.pause_mission(mission_id)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "skipped")
        self.assertEqual(len(runner.calls), 0)

    def test_reviews_do_not_count_as_runs_or_checkpoints(self):
        runner = ScriptedReviewRunner()
        factory = ScriptedFactory(scripts=[
            [{"op": "add_task", "text": "real progress"}], [], [],
        ])
        engine = self.engine(factory, review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        checkpoint = self.store.latest_checkpoint(mission_id)
        runs = self.store.get_mission(mission_id)["runs"]
        result = self.run_next(engine, mission_id)
        self.assertEqual(result["outcome"], "review_continue")
        self.assertEqual(self.store.get_mission(mission_id)["runs"], runs)
        self.assertEqual(
            self.store.latest_checkpoint(mission_id)["checkpoint_id"],
            checkpoint["checkpoint_id"],
        )


class TestReviewCrashRecovery(ReviewCase):
    def test_hung_review_is_abandoned_and_reconciled(self):
        def hanging_runner(mission, context, signals):
            self.clock.advance(
                review_mod.REVIEW_DEFAULTS["wall_seconds"] + 301
            )
            return json.dumps(
                {"criteria": [], "action": "continue", "rationale": "late"}
            ), {}, ""

        engine = self.engine(
            ScriptedFactory(), review_runner=hanging_runner
        )
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        result = self.run_next(engine, mission_id)
        self.assertEqual(result["outcome"], "abandoned")
        self.assertIsNone(self.store.latest_review(mission_id))
        report = self.store.reconcile()
        self.assertEqual(report["sessions_abandoned"], 1)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        status = self.store.budget_status(mission["root_scope_id"])
        self.assertEqual(status["tokens"]["reserved"], 0)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)


class TestReviewSurfaces(ReviewCase):
    def test_mission_detail_renders_latest_review(self):
        from conch.kernel.views import mission_detail

        runner = ScriptedReviewRunner(verdicts=[{
            "criteria": [
                {"criterion": "digest exists", "verdict": "on-track",
                 "evidence": "two digests produced"},
            ],
            "action": "continue", "rationale": "healthy",
        }])
        factory = ScriptedFactory(scripts=[
            [{"op": "add_task", "text": "real progress"}], [], [],
        ])
        engine = self.engine(factory, review_runner=runner)
        mission_id = self.make_mission(engine)
        self.run_work_sessions(engine, mission_id, 3)
        self.run_next(engine, mission_id)
        detail = mission_detail(
            self.store, self.store.get_mission(mission_id)
        )
        self.assertEqual(detail["review"]["action"], "continue")
        self.assertEqual(
            detail["review"]["criteria"][0]["verdict"], "on-track"
        )
        self.assertFalse(detail["review"]["stalled"])

    def test_default_runner_uses_weak_model(self):
        seen = {}

        def weak_raw_fn(config, messages, tools):
            seen["model"] = config.get("chat_model")
            seen["tools"] = tools
            seen["system"] = messages[0]["content"]
            return {
                "content": json.dumps({
                    "criteria": [], "action": "continue", "rationale": "ok",
                }),
                "tool_calls": None,
                "_usage": {"input_tokens": 5, "output_tokens": 5},
                "_model": "qwen3-8b",
            }

        config = {"provider": "ollama", "chat_model": "qwen3.5:122b",
                  "weak_model": "qwen3-8b"}
        runner = review_mod.default_runner(config)
        with patch.dict(
            "conch.providers.RAW_FNS", {"ollama": weak_raw_fn}
        ), patch(
            "conch.providers.validate_model_for_provider",
            return_value=(True, ""),
        ):
            reply, usage, error = runner({}, "context", {})
        self.assertEqual(error, "")
        self.assertEqual(seen["model"], "qwen3-8b")
        self.assertIsNone(seen["tools"])  # reviews never expose tools
        self.assertIn("mission critic", seen["system"])
        self.assertEqual(
            review_mod.parse_verdict(reply)["action"], "continue"
        )

    def test_parse_verdict_tolerates_prose_and_rejects_garbage(self):
        wrapped = (
            "Here is my verdict:\n"
            '{"criteria": [{"criterion": "c", "verdict": "met",'
            ' "evidence": "e"}], "action": "replan",'
            ' "rationale": "r", "plan": ["s1"]}'
        )
        verdict = review_mod.parse_verdict(wrapped)
        self.assertEqual(verdict["action"], "re-plan")
        self.assertEqual(verdict["plan"], ["s1"])
        self.assertIsNone(review_mod.parse_verdict("no json here"))
        self.assertIsNone(
            review_mod.parse_verdict('{"action": "explode"}')
        )


if __name__ == "__main__":
    unittest.main()
