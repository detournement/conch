"""Event-driven mission wakes (always-on daemon work item).

Proven here: external events routed through the kernel inbox wake parked
missions immediately — a ``waiting_input`` mission whose input arrives runs
its session in the same daemon tick with no timer wait; ``waiting_timer``
missions wake early on addressed events; paused missions and missions
parked on an approval are never woken by unsolicited events; delivery is
idempotent on the event key; the ``event.post`` control op exposes the same
API over the daemon socket; and a Capitol-supervision wake observed in a
tick fires the woken mission's session inside that same tick.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.kernel import control
from conch.kernel.daemon import EdgeDaemon
from conch.kernel.model import KernelError, MissionState


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def make_input_requesting_factory():
    """Session factory whose first run parks the mission on user input."""
    calls = {"n": 0}

    def factory(mission, messages, control_client, caps):
        calls["n"] += 1
        if calls["n"] == 1:
            control_client.call_tool(
                "mission_control",
                {"op": "request_input", "text": "which repo?"},
            )
        return f"run {calls['n']}", {"total_tokens": 3}

    factory.calls = calls
    return factory


def plain_factory(mission, messages, control_client, caps):
    return "session output", {"total_tokens": 3}


class EventWakeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Isolated XDG: nothing here may ever touch the live user kernel.
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.kernel_dir = self.root / "kernel"
        self.socket_path = self.root / "run" / "edge.sock"
        self.clock = FakeClock()

    def make_daemon(self, config=None, session_factory=plain_factory):
        daemon = EdgeDaemon(
            config or {"provider": "openai"},
            kernel_dir=self.kernel_dir,
            state_dir=self.root,
            socket_path=self.socket_path,
            clock=self.clock,
            session_factory=session_factory,
        )
        self.addCleanup(daemon.shutdown)
        daemon.start()
        return daemon

    def event_kinds(self, daemon, mission_id):
        return [
            event["kind"]
            for event in daemon.store.event_tail(mission_id, limit=200)
        ]

    def park_waiting_input(self, daemon):
        """Create a mission and run it into waiting_input."""
        mission_id = daemon.engine.create_mission({
            "goal": "needs input", "budgets": {},
            "cadence_seconds": 3600,
        }, activate=True)
        daemon.tick()
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.WAITING_INPUT,
        )
        return mission_id


class TestWaitingInputWake(EventWakeCase):
    def test_input_fires_session_in_same_tick_no_timer_wait(self):
        daemon = self.make_daemon(
            session_factory=make_input_requesting_factory()
        )
        mission_id = self.park_waiting_input(daemon)
        result = daemon.engine.provide_input(
            mission_id, "use repo X", source="test"
        )
        self.assertTrue(result["woken"])
        self.assertFalse(result["duplicate"])
        # No clock advance: the wake must not depend on any timer.
        stats = daemon.tick()
        self.assertEqual(stats["fired"], 0, "no timer fired for this wake")
        self.assertEqual(stats["sessions"], 1)
        mission = daemon.store.get_mission(mission_id)
        self.assertEqual(mission["runs"], 2)
        # Kernel events prove the order: input received -> transition to
        # ready -> the session started, all with zero clock movement.
        kinds = self.event_kinds(daemon, mission_id)
        inbox_at = kinds.index("inbox_received")
        self.assertIn("mission_transitioned", kinds[inbox_at:])
        self.assertIn("session_started", kinds[inbox_at:])
        self.assertIn("session_checkpointed", kinds[inbox_at:])

    def test_input_reaches_next_session_context(self):
        seen = {}

        def recording_factory(mission, messages, control_client, caps):
            recording_factory.calls = getattr(
                recording_factory, "calls", 0
            ) + 1
            if recording_factory.calls == 1:
                control_client.call_tool(
                    "mission_control",
                    {"op": "request_input", "text": "which repo?"},
                )
            else:
                seen["context"] = messages[-1]["content"]
            return "ok", {}

        daemon = self.make_daemon(session_factory=recording_factory)
        mission_id = self.park_waiting_input(daemon)
        daemon.engine.provide_input(mission_id, "use the conch repo")
        daemon.tick()
        self.assertIn("use the conch repo", seen["context"])


class TestWaitingTimerWake(EventWakeCase):
    def test_scheduled_mission_wakes_early_on_input(self):
        daemon = self.make_daemon()
        control.request("schedule.add", {
            "prompt": "check disk", "interval": 600,
        }, socket_path=self.socket_path)
        mission = daemon.store.list_missions()[0]
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        result = daemon.engine.provide_input(
            mission["mission_id"], "run it now please"
        )
        self.assertTrue(result["woken"])
        stats = daemon.tick()
        self.assertEqual(stats["fired"], 0)
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(
            daemon.store.get_mission(mission["mission_id"])["runs"], 1
        )

    def test_deliver_event_wakes_and_dedupes(self):
        daemon = self.make_daemon()
        mission_id = daemon.engine.create_mission({
            "goal": "watch the build", "budgets": {},
            "cadence_seconds": 3600,
        }, activate=True)
        daemon.tick()  # first session, back to waiting_timer
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.WAITING_TIMER,
        )
        result = daemon.engine.deliver_event(
            "webhook", "build:1234", {"text": "build 1234 finished"},
            mission_id,
        )
        self.assertTrue(result["woken"])
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.READY,
        )
        # Redelivery with the same key: recorded once, no second wake.
        duplicate = daemon.engine.deliver_event(
            "webhook", "build:1234", {"text": "build 1234 finished"},
            mission_id,
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(duplicate["woken"])
        kinds = self.event_kinds(daemon, mission_id)
        self.assertEqual(kinds.count("inbox_received"), 1)
        ok, detail = daemon.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_wake_false_records_without_waking(self):
        daemon = self.make_daemon()
        control.request("schedule.add", {
            "prompt": "quiet", "interval": 600,
        }, socket_path=self.socket_path)
        mission = daemon.store.list_missions()[0]
        result = daemon.engine.deliver_event(
            "watch", "note:1", {"text": "fyi"}, mission["mission_id"],
            wake=False,
        )
        self.assertFalse(result["woken"])
        self.assertEqual(
            daemon.store.get_mission(mission["mission_id"])["status"],
            MissionState.WAITING_TIMER,
        )

    def test_unaddressed_event_records_without_wake(self):
        daemon = self.make_daemon()
        result = daemon.engine.deliver_event(
            "webhook", "global:1", {"text": "hello"}
        )
        self.assertFalse(result["woken"])
        self.assertFalse(result["duplicate"])

    def test_bad_events_fail_closed(self):
        daemon = self.make_daemon()
        with self.assertRaises(KernelError):
            daemon.engine.deliver_event("webhook", "", {"text": "x"})
        with self.assertRaises(KernelError):
            daemon.engine.deliver_event(
                "webhook", "k1", {"text": "x"}, "msn-missing"
            )


class TestWakeGuards(EventWakeCase):
    def test_paused_mission_never_woken_by_events(self):
        daemon = self.make_daemon()
        mission_id = daemon.engine.create_mission({
            "goal": "paused", "budgets": {}, "cadence_seconds": 600,
        }, activate=True)
        daemon.engine.pause_mission(mission_id)
        result = daemon.engine.deliver_event(
            "webhook", "pause:1", {"text": "wake up"}, mission_id
        )
        self.assertFalse(result["woken"])
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.PAUSED,
        )

    def test_waiting_approval_never_woken_by_events(self):
        daemon = self.make_daemon()
        mission_id = daemon.engine.create_mission({
            "goal": "gated", "budgets": {}, "cadence_seconds": 600,
        }, activate=True)
        daemon.store.transition_mission(mission_id, MissionState.ACTIVE)
        daemon.store.transition_mission(
            mission_id, MissionState.WAITING_APPROVAL
        )
        result = daemon.engine.deliver_event(
            "webhook", "gate:1", {"text": "just do it"}, mission_id
        )
        self.assertFalse(result["woken"],
                         "an event must never bypass an approval gate")
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.WAITING_APPROVAL,
        )

    def test_terminal_mission_never_woken(self):
        daemon = self.make_daemon()
        mission_id = daemon.engine.create_mission({
            "goal": "done", "budgets": {}, "cadence_seconds": 600,
        }, activate=True)
        daemon.engine.abort_mission(mission_id)
        result = daemon.engine.deliver_event(
            "webhook", "late:1", {"text": "too late"}, mission_id
        )
        self.assertFalse(result["woken"])


class TestControlOps(EventWakeCase):
    def test_event_post_over_socket(self):
        daemon = self.make_daemon()
        control.request("schedule.add", {
            "prompt": "socket wake", "interval": 600,
        }, socket_path=self.socket_path)
        mission = daemon.store.list_missions()[0]
        result = control.request("event.post", {
            "source": "webhook", "key": "sock:1",
            "payload": {"text": "external event"},
            "mission_id": mission["mission_id"],
        }, socket_path=self.socket_path)
        self.assertTrue(result["woken"])
        self.assertEqual(
            daemon.store.get_mission(mission["mission_id"])["status"],
            MissionState.READY,
        )

    def test_event_post_fails_closed_on_bad_payload(self):
        self.make_daemon()
        with self.assertRaises(control.ControlError):
            control.request("event.post", {
                "source": "webhook", "key": "sock:2", "payload": "text",
            }, socket_path=self.socket_path)
        with self.assertRaises(control.ControlError):
            control.request("event.post", {
                "source": "webhook", "key": "", "payload": {"a": 1},
            }, socket_path=self.socket_path)

    def test_mission_input_op_reports_wake(self):
        daemon = self.make_daemon(
            session_factory=make_input_requesting_factory()
        )
        mission_id = self.park_waiting_input(daemon)
        result = control.request("mission.input", {
            "mission_id": mission_id, "text": "the answer",
        }, socket_path=self.socket_path)
        self.assertTrue(result["woken"])


class TestSameTickScheduling(EventWakeCase):
    def test_capitol_wake_runs_session_in_same_tick(self):
        """A supervision pass that wakes a mission must see that mission's
        session run before the tick ends — supervision is ordered ahead of
        the session slot."""
        config = {
            "provider": "openai",
            "capitol_base_url": "http://localhost:9",
            "capitol_org": "org", "capitol_agent": "agent",
            "capitol_poll_seconds": 1,
        }
        daemon = self.make_daemon(config=config)
        mission_id = daemon.engine.create_mission({
            "goal": "capitol-bound", "budgets": {}, "cadence_seconds": 3600,
        }, activate=True)
        daemon.tick()  # first session; mission parks on its timer
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.WAITING_TIMER,
        )

        engine = daemon.engine

        class WakingSupervisor:
            def tick(self):
                woken = engine.wake_mission(mission_id, reason="capitol")
                return {"woken": int(woken)}

        daemon._capitol = WakingSupervisor()
        self.clock.advance(2)  # only the capitol poll cadence, no timers due
        stats = daemon.tick()
        self.assertEqual(stats["fired"], 0)
        self.assertEqual(stats.get("capitol_woken"), 1)
        self.assertEqual(stats["sessions"], 1,
                         "the woken session must fire in the same tick")


if __name__ == "__main__":
    unittest.main()
