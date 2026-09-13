"""Mission engine gates (Swarm Phase 1).

Proven here: activation semantics, bounded rehydration regardless of
journal size, checkpoint-at-boundary session flows (budget commit +
transition + next wake + notification in one transaction), the
mission_control tool (staged next wake / completion / input requests and
immediate durable plan/note/task ops), STOP honored at session start and
before each tool round through the fail-closed required-policy layer, hard
budget enforcement failing missions that cannot afford a session, and
crash/abandon recovery with no duplicate effects.
"""

import tempfile
import unittest
from pathlib import Path

from conch.kernel.engine import (
    MAX_CONTEXT_CHARS,
    MissionControlClient,
    MissionEngine,
)
from conch.kernel.model import MissionState
from conch.kernel.store import MissionStore
from conch.policy import evaluate_required_policy


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class ScriptedFactory:
    """Fake session runner: records the rehydrated messages and optionally
    drives the mission_control tool exactly as a model would."""

    def __init__(self, reply="did the work", usage=None, script=None,
                 exception=None):
        self.reply = reply
        self.usage = usage or {"total_tokens": 1234}
        self.script = script or []
        self.exception = exception
        self.calls = []

    def __call__(self, mission, messages, control, caps):
        self.calls.append({
            "mission": mission, "messages": messages, "caps": caps,
        })
        for op in self.script:
            control.call_tool("mission_control", op)
        if self.exception is not None:
            raise self.exception
        return self.reply, self.usage


BASE_SPEC = {
    "goal": "summarize repo activity daily",
    "success_criteria": ["a fresh digest exists"],
    "budgets": {"tokens": 100000, "sessions": 50},
    "cadence_seconds": 86400,
}


class EngineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.kernel_dir = Path(self._tmp.name) / "kernel"
        self.clock = FakeClock()
        self.store = MissionStore(
            self.kernel_dir / "kernel.db", clock=self.clock
        )
        self.addCleanup(lambda: self.store.close())

    def engine(self, factory=None, config=None):
        # Reviews and consolidation have their own suites
        # (test_mission_review / test_mission_memory); the base engine
        # config keeps them off so work-session mechanics stay isolated.
        return MissionEngine(
            self.store, config or {
                "provider": "openai", "model": "gpt-4o",
                "mission_reviews": "false",
                "mission_consolidation": "false",
            },
            holder="test-daemon", session_factory=factory,
            kernel_dir=self.kernel_dir,
        )

    def make_ready(self, engine, **overrides):
        spec = dict(BASE_SPEC)
        spec.update(overrides)
        return engine.create_mission(spec, activate=True)


class TestActivation(EngineCase):
    def test_standard_mission_ready_with_wake_timer(self):
        engine = self.engine()
        mission_id = self.make_ready(engine)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["interval_seconds"], 86400)
        self.assertEqual(timer["due_at"], self.clock() + 86400)

    def test_scheduled_prompt_parks_until_first_cadence(self):
        engine = self.engine()
        mission_id = engine.create_mission({
            "goal": "run: check disk space",
            "kind": "scheduled_prompt", "prompt": "check disk space",
            "cadence_seconds": 3600, "budgets": {},
        })
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["due_at"], self.clock() + 3600)

    def test_draft_only_when_not_activated(self):
        engine = self.engine()
        mission_id = engine.create_mission(dict(BASE_SPEC), activate=False)
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.DRAFT
        )


class TestSessionFlow(EngineCase):
    def test_happy_path_checkpoints_and_waits(self):
        factory = ScriptedFactory(reply="digest written")
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.WAITING_TIMER)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        self.assertEqual(mission["runs"], 1)
        checkpoint = self.store.latest_checkpoint(mission_id)
        self.assertIn("digest written", checkpoint["summary"])
        status = self.store.budget_status(mission["root_scope_id"])
        self.assertEqual(status["sessions"]["committed"], 1)
        self.assertEqual(status["tokens"]["committed"], 1234)
        self.assertEqual(status["tokens"]["reserved"], 0)
        # default cadence set the next wake
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["due_at"], self.clock() + 86400)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_model_sets_next_wake(self):
        factory = ScriptedFactory(script=[
            {"op": "set_next_wake", "seconds": 7200},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        engine.run_session(mission_id)
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["due_at"], self.clock() + 7200)

    def test_complete_mission_terminal_and_notifies(self):
        factory = ScriptedFactory(script=[
            {"op": "complete_mission", "text": "all criteria met"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.SUCCEEDED)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.SUCCEEDED)
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["status"], "cancelled")
        outbox = self.store.list_outbox(mission_id=mission_id)
        self.assertEqual(len(outbox), 1)
        self.assertIn("succeeded", outbox[0]["payload"])

    def test_fail_mission(self):
        factory = ScriptedFactory(script=[
            {"op": "fail_mission", "text": "cannot proceed"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.FAILED)

    def test_request_input_parks_and_resume_via_input(self):
        factory = ScriptedFactory(script=[
            {"op": "request_input", "text": "which repo should I watch?"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.WAITING_INPUT)
        outbox = self.store.list_outbox(mission_id=mission_id)
        self.assertTrue(
            any("needs your input" in row["payload"] for row in outbox)
        )
        engine.provide_input(mission_id, "watch conch itself")
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        # the next session sees the answer in its bounded context
        factory2 = ScriptedFactory()
        engine2 = self.engine(factory2)
        engine2.run_session(mission_id)
        context = factory2.calls[0]["messages"][1]["content"]
        self.assertIn("watch conch itself", context)

    def test_durable_ops_apply_immediately(self):
        factory = ScriptedFactory(script=[
            {"op": "update_plan", "steps": ["scan repo", "write digest"]},
            {"op": "note", "text": "repo has 3 new commits"},
            {"op": "add_task", "text": "handle the merge conflict"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        engine.run_session(mission_id)
        plan = self.store.latest_plan(mission_id)
        self.assertEqual(
            plan["content"]["steps"], ["scan repo", "write digest"]
        )
        self.assertEqual(len(self.store.open_tasks(mission_id)), 1)

    def test_factory_exception_records_error_and_backs_off(self):
        factory = ScriptedFactory(exception=RuntimeError("provider down"))
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.WAITING_TIMER)
        self.assertIn("provider down", result["error"])
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        self.assertIn("provider down", mission["last_error"])
        # bounded backoff rather than a hot retry loop
        timer = self.store.find_timer(mission_id, "wake")
        self.assertGreaterEqual(timer["due_at"], self.clock() + 300)

    def test_usage_error_recorded(self):
        factory = ScriptedFactory(usage={"error": "backend unreachable"})
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertIn("backend unreachable", result["error"])

    def test_notify_sessions_sends_the_digest_each_checkpoint(self):
        factory = ScriptedFactory(reply="today: 3 commits, tree clean")
        engine = self.engine(factory)
        mission_id = self.make_ready(engine, notify="sessions")
        engine.run_session(mission_id)
        outbox = self.store.list_outbox(mission_id=mission_id)
        self.assertEqual(len(outbox), 1)
        self.assertIn("session digest", outbox[0]["payload"])
        self.assertIn("3 commits", outbox[0]["payload"])

    def test_milestones_notify_stays_quiet_on_routine_checkpoints(self):
        factory = ScriptedFactory(reply="routine work")
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)  # default notify=milestones
        engine.run_session(mission_id)
        self.assertEqual(self.store.list_outbox(mission_id=mission_id), [])

    def test_scheduled_prompt_notifies_each_run(self):
        factory = ScriptedFactory(reply="disk is 42% full")
        engine = self.engine(factory)
        mission_id = engine.create_mission({
            "goal": "run: check disk", "kind": "scheduled_prompt",
            "prompt": "check disk", "cadence_seconds": 3600, "budgets": {},
        })
        # wake it as the timer would
        self.clock.advance(3601)
        timer = self.store.find_timer(mission_id, "wake")
        self.store.claim_due_timers("test-daemon")
        self.store.fire_timer(
            timer["timer_id"], timer["generation"], holder="test-daemon"
        )
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.WAITING_TIMER)
        outbox = self.store.list_outbox(mission_id=mission_id)
        self.assertEqual(len(outbox), 1)
        self.assertIn("disk is 42% full", outbox[0]["payload"])
        # the prompt rides the user message, fresh each session
        self.assertEqual(
            factory.calls[0]["messages"][1]["content"], "check disk"
        )

    def test_run_once_scheduled_prompt_succeeds_after_first_run(self):
        factory = ScriptedFactory()
        engine = self.engine(factory)
        mission_id = engine.create_mission({
            "goal": "run once: cleanup", "kind": "scheduled_prompt",
            "prompt": "cleanup", "run_once": True,
            "cadence_seconds": 60, "budgets": {},
        })
        self.clock.advance(61)
        timer = self.store.find_timer(mission_id, "wake")
        self.store.claim_due_timers("test-daemon")
        self.store.fire_timer(
            timer["timer_id"], timer["generation"], holder="test-daemon"
        )
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.SUCCEEDED)


class TestStopEnforcement(EngineCase):
    def test_global_stop_file_blocks_session_start(self):
        engine = self.engine(ScriptedFactory())
        mission_id = self.make_ready(engine)
        engine.stop_file().parent.mkdir(parents=True, exist_ok=True)
        engine.stop_file().write_text("halt\n")
        self.assertEqual(engine.run_ready_sessions(), [])
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "skipped")
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.READY
        )

    def test_mission_stop_flag_parks_at_session_start(self):
        engine = self.engine(ScriptedFactory())
        mission_id = self.make_ready(engine)
        self.store.set_stop(mission_id, True)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "paused")
        self.assertEqual(
            self.store.get_mission(mission_id)["status"],
            MissionState.PAUSED,
        )

    def test_stop_denies_tool_rounds_through_required_policy(self):
        """The registered fail-closed check re-evaluates STOP (file + flag)
        and the wall deadline before every tool round."""
        engine = self.engine()
        mission_id = self.make_ready(engine)
        observations = {}

        def probing_factory(mission, messages, control, caps):
            # mid-turn, tool dispatch consults required policy
            observations["before"] = evaluate_required_policy(
                "pre_tool_use", {"tool": "local_shell", "arguments": {}}
            )
            engine.stop_file().write_text("halt\n")
            observations["after_stop_file"] = evaluate_required_policy(
                "pre_tool_use", {"tool": "local_shell", "arguments": {}}
            )
            engine.stop_file().unlink()
            self.clock.advance(BASE_SPEC.get("session_wall", 0) or 999999)
            observations["after_deadline"] = evaluate_required_policy(
                "pre_tool_use", {"tool": "local_shell", "arguments": {}}
            )
            return "done", {}

        engine._session_factory = probing_factory
        engine.run_session(mission_id)
        self.assertTrue(observations["before"].allowed)
        self.assertFalse(observations["after_stop_file"].allowed)
        self.assertIn("STOP", observations["after_stop_file"].reason)
        self.assertFalse(observations["after_deadline"].allowed)
        self.assertIn("wall", observations["after_deadline"].reason)
        # the check is unregistered once the session ends
        self.assertTrue(
            evaluate_required_policy(
                "pre_tool_use", {"tool": "local_shell", "arguments": {}}
            ).allowed
        )

    def test_mid_session_mission_stop_denies_tools(self):
        engine = self.engine()
        mission_id = self.make_ready(engine)
        observations = {}

        def stopping_factory(mission, messages, control, caps):
            self.store.set_stop(mission_id, True)
            observations["after_flag"] = evaluate_required_policy(
                "pre_tool_use", {"tool": "local_shell", "arguments": {}}
            )
            return "stopped early", {}

        engine._session_factory = stopping_factory
        engine.run_session(mission_id)
        self.assertFalse(observations["after_flag"].allowed)


class TestBudgetEnforcement(EngineCase):
    def test_mission_fails_when_it_cannot_afford_a_session(self):
        factory = ScriptedFactory(usage={"total_tokens": 10})
        engine = self.engine(factory)
        mission_id = self.make_ready(engine, budgets={"sessions": 1})
        first = engine.run_session(mission_id)
        self.assertEqual(first["outcome"], MissionState.WAITING_TIMER)
        # wake it again
        self.clock.advance(86401)
        timer = self.store.find_timer(mission_id, "wake")
        self.store.claim_due_timers("test-daemon")
        self.store.fire_timer(
            timer["timer_id"], timer["generation"], holder="test-daemon"
        )
        second = engine.run_session(mission_id)
        self.assertEqual(second["outcome"], "failed")
        self.assertIn("budget exhausted", second["error"])
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.FAILED)

    def test_token_commit_capped_at_reservation(self):
        factory = ScriptedFactory(usage={"total_tokens": 999999})
        engine = self.engine(factory)
        mission_id = self.make_ready(engine, budgets={"tokens": 5000})
        engine.run_session(mission_id)
        mission = self.store.get_mission(mission_id)
        status = self.store.budget_status(mission["root_scope_id"])
        self.assertLessEqual(status["tokens"]["committed"], 5000)

    def test_chat_turn_usage_shape_is_counted(self):
        """chat_turn reports input_tokens/output_tokens — the engine must
        commit real usage from that shape, not zero."""
        factory = ScriptedFactory(
            usage={"input_tokens": 800, "output_tokens": 150, "model": "m"}
        )
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        engine.run_session(mission_id)
        mission = self.store.get_mission(mission_id)
        status = self.store.budget_status(mission["root_scope_id"])
        self.assertEqual(status["tokens"]["committed"], 950)


class TestBoundedRehydration(EngineCase):
    def test_context_bounded_regardless_of_journal_size(self):
        engine = self.engine()
        mission_id = self.make_ready(engine)
        for i in range(3000):
            self.store.record_note(
                mission_id, f"observation {i}: " + "x" * 200
            )
        self.store.record_checkpoint(
            mission_id, "checkpoint with a large body " + "y" * 20000
        )
        mission = self.store.get_mission(mission_id)
        context = engine.build_context(mission)
        self.assertLessEqual(len(context), MAX_CONTEXT_CHARS)
        self.assertIn("Goal:", context)
        self.assertIn("Remaining budgets", context)
        self.assertGreater(self.store.event_count(mission_id), 3000)

    def test_context_contains_plan_tasks_and_checkpoint(self):
        engine = self.engine()
        mission_id = self.make_ready(engine)
        self.store.record_plan(mission_id, {"steps": ["alpha", "beta"]})
        self.store.create_task(mission_id, "find the regression")
        self.store.record_checkpoint(mission_id, "yesterday: wrote digest")
        context = engine.build_context(self.store.get_mission(mission_id))
        self.assertIn("alpha", context)
        self.assertIn("find the regression", context)
        self.assertIn("yesterday: wrote digest", context)


class TestCrashRecovery(EngineCase):
    def test_checkpoint_refused_after_lease_expiry_then_reconcile(self):
        """kill -9 equivalence: the session outlives its lease (e.g. the
        machine slept); its late checkpoint is refused, reconcile repairs,
        and the retried session commits exactly once."""
        engine = self.engine()
        mission_id = self.make_ready(engine)

        def hanging_factory(mission, messages, control, caps):
            # the session hangs past its wall+grace lease
            self.clock.advance(
                mission["spec"]["session_wall_seconds"] + 301
            )
            return "too late", {"total_tokens": 5}

        engine._session_factory = hanging_factory
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], "abandoned")
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.ACTIVE)
        report = self.store.reconcile()
        self.assertEqual(report["sessions_abandoned"], 1)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        self.assertEqual(mission["runs"], 0)  # nothing double-counted
        status = self.store.budget_status(mission["root_scope_id"])
        self.assertEqual(status["sessions"]["reserved"], 0)
        engine._session_factory = ScriptedFactory()
        second = engine.run_session(mission_id)
        self.assertEqual(second["outcome"], MissionState.WAITING_TIMER)
        self.assertEqual(self.store.get_mission(mission_id)["runs"], 1)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)


class TestApprovalWake(EngineCase):
    def test_deciding_approval_wakes_waiting_mission(self):
        factory = ScriptedFactory()
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        grant = self.store.request_approval(
            mission_id, "publish", {"item": 1}, ttl_seconds=600
        )
        # park the mission on the approval (as a session would)
        self.store.start_session(mission_id, "ses-x", "test-daemon", {})
        self.store.checkpoint_session(
            mission_id, "ses-x", "test-daemon", "waiting",
            MissionState.WAITING_APPROVAL,
        )
        engine.decide_approval(
            grant["approval_id"], "approve", nonce=grant["nonce"],
            origin_channel="local", decided_by="operator",
        )
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.READY
        )


class TestMissionControlClient(EngineCase):
    def test_unknown_op_reports_not_raises(self):
        engine = self.engine()
        mission_id = self.make_ready(engine)
        client = MissionControlClient(self.store, mission_id, "ses-1")
        result = client.call_tool("mission_control", {"op": "explode"})
        self.assertIn("Unknown", result["content"][0]["text"])

    def test_kernel_errors_surface_as_tool_text(self):
        engine = self.engine()
        mission_id = self.make_ready(engine)
        client = MissionControlClient(self.store, mission_id, "ses-1")
        result = client.call_tool(
            "mission_control", {"op": "complete_task", "task_id": "task-x"}
        )
        self.assertIn("error", result["content"][0]["text"])


class TestModelCompletionKnob(EngineCase):
    """allow_model_completion: cadence missions (recurring schedule, no
    success criteria) default to refusing complete_mission — a weak model
    must not self-complete a run-forever digest/watch. The denial is a
    journaled fact and a steering tool message; the mission stays on its
    schedule."""

    def test_cadence_mission_denies_completion_and_journals(self):
        factory = ScriptedFactory(script=[
            {"op": "complete_mission", "text": "digest looks done to me"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine, success_criteria=[])
        result = engine.run_session(mission_id)
        # The completion never staged: the session parked on its cadence.
        self.assertEqual(result["outcome"], MissionState.WAITING_TIMER)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        events = self.store.events_since(
            mission_id, 0, ("completion_denied",)
        )
        self.assertEqual(len(events), 1)
        self.assertIn("cadence", events[0]["data"]["reason"])
        # The new event kind replays cleanly.
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_denial_tool_message_steers_and_stages_nothing(self):
        engine = self.engine()
        mission_id = self.make_ready(engine, success_criteria=[])
        client = MissionControlClient(self.store, mission_id, "ses-1")
        result = client.call_tool(
            "mission_control", {"op": "complete_mission", "text": "done"}
        )
        text = result["content"][0]["text"]
        self.assertIn("denied", text)
        self.assertIn("set_next_wake", text)
        self.assertIsNone(client.staged["outcome"])

    def test_explicit_allow_overrides_the_cadence_default(self):
        factory = ScriptedFactory(script=[
            {"op": "complete_mission", "text": "operator opted in"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(
            engine, success_criteria=[], allow_model_completion=True
        )
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.SUCCEEDED)

    def test_success_criteria_missions_still_complete(self):
        # BASE_SPEC carries success criteria — completion semantics exist,
        # so the default stays permissive (also covered by
        # test_complete_mission_terminal_and_notifies).
        factory = ScriptedFactory(script=[
            {"op": "complete_mission", "text": "all criteria met"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.SUCCEEDED)

    def test_fail_mission_is_not_gated(self):
        # The knob governs self-declared success only; a genuinely broken
        # cadence mission may still fail itself.
        factory = ScriptedFactory(script=[
            {"op": "fail_mission", "text": "credentials revoked"},
        ])
        engine = self.engine(factory)
        mission_id = self.make_ready(engine, success_criteria=[])
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.FAILED)

    def test_spec_update_can_turn_completion_off_for_live_missions(self):
        # The operator path used on the live repo-digest mission: update
        # the spec through the store API, then the gate holds.
        engine = self.engine()
        mission_id = self.make_ready(engine)  # criteria => allowed
        mission = self.store.get_mission(mission_id)
        spec = dict(mission["spec"])
        spec["allow_model_completion"] = False
        self.store.update_spec(mission_id, spec)
        client = MissionControlClient(self.store, mission_id, "ses-2")
        result = client.call_tool(
            "mission_control", {"op": "complete_mission", "text": "done"}
        )
        self.assertIn("denied", result["content"][0]["text"])
        self.assertEqual(
            len(self.store.events_since(
                mission_id, 0, ("completion_denied",)
            )),
            1,
        )


if __name__ == "__main__":
    unittest.main()
