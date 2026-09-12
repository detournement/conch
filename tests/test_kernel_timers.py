"""Timer gates (Swarm Phase 1): fake-clock weeks, misfire policies, and
kill/restart at every claim/fire boundary with no duplicate effects.

The daemon is deliberately absent here — these tests drive the store's
claim → fire → advance protocol directly, simulating crashes by dropping
claims (kill before fire) and by reopening the store (kill after commit).
Timer fires are exactly-once *ledger effects*: each scheduled occurrence
appears as at most one ``timer_fired`` event no matter how many claim
attempts, restarts, or stale holders race for it.
"""

import tempfile
import unittest
from pathlib import Path

from conch.kernel.model import (
    KernelError,
    MisfirePolicy,
    MissionState,
    StaleGenerationError,
)
from conch.kernel.store import MissionStore


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


DAY = 86400


class TimerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "kernel.db"
        self.clock = FakeClock()
        self.store = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(lambda: self.store.close())

    def make_waiting_mission(self, **spec_overrides):
        spec = {"goal": "tick", "budgets": {}, "cadence_seconds": DAY}
        spec.update(spec_overrides)
        mission_id = self.store.create_mission(spec)
        self.store.transition_mission(mission_id, MissionState.READY)
        self.store.start_session(mission_id, "ses-0", "d", {})
        self.store.checkpoint_session(
            mission_id, "ses-0", "d", "bootstrap", MissionState.WAITING_TIMER
        )
        return mission_id

    def fired_events(self, mission_id):
        return [
            event for event in self.store.event_tail(mission_id, limit=10000)
            if event["kind"] == "timer_fired"
        ]

    def process_due(self, holder="daemon-a", lease_seconds=120.0):
        """One daemon pass: claim due timers and fire each claim."""
        results = []
        for claim in self.store.claim_due_timers(
            holder, lease_seconds=lease_seconds
        ):
            results.append(self.store.fire_timer(
                claim["timer_id"], claim["generation"], holder=holder
            ))
        return results


class TestOneShotTimers(TimerCase):
    def test_fires_once_then_completes(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "once", self.clock() + 60
        )
        self.assertEqual(self.process_due(), [])
        self.clock.advance(61)
        results = self.process_due()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["fires"]), 1)
        timer = self.store.get_timer(timer_id)
        self.assertEqual(timer["status"], "completed")
        # the mission woke exactly once
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.READY
        )
        # further passes never fire it again
        self.clock.advance(DAY)
        self.assertEqual(self.process_due(), [])
        self.assertEqual(len(self.fired_events(mission_id)), 1)

    def test_double_fire_rejected(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "once", self.clock() + 10
        )
        self.clock.advance(11)
        claims = self.store.claim_due_timers("daemon-a")
        self.store.fire_timer(timer_id, claims[0]["generation"], holder="daemon-a")
        with self.assertRaises(StaleGenerationError):
            self.store.fire_timer(
                timer_id, claims[0]["generation"], holder="daemon-a"
            )

    def test_one_shot_skip_policy_late(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "once", self.clock() + 10,
            misfire_policy=MisfirePolicy.SKIP,
        )
        self.clock.advance(DAY)  # far beyond the misfire grace
        results = self.process_due()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["skipped"])
        self.assertEqual(self.fired_events(mission_id), [])
        self.assertEqual(
            self.store.get_timer(timer_id)["status"], "cancelled"
        )
        # mission stays waiting — nothing woke it
        self.assertEqual(
            self.store.get_mission(mission_id)["status"],
            MissionState.WAITING_TIMER,
        )


class TestRecurringAndMisfires(TimerCase):
    def test_weeks_of_daily_fires_exactly_once_each(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + DAY, interval_seconds=DAY
        )
        for _ in range(21):  # three weeks, tick every 6 hours
            for _ in range(4):
                self.clock.advance(DAY / 4)
                for result in self.process_due():
                    self.assertEqual(len(result["fires"]), 1)
                # a woken mission runs a session and goes back to waiting
                mission = self.store.get_mission(mission_id)
                if mission["status"] == MissionState.READY:
                    session = f"ses-{mission['runs'] + 1}"
                    self.store.start_session(mission_id, session, "d", {})
                    self.store.checkpoint_session(
                        mission_id, session, "d", "tick",
                        MissionState.WAITING_TIMER,
                    )
        fired = self.fired_events(mission_id)
        self.assertEqual(len(fired), 21)
        # every scheduled occurrence fired exactly once
        scheduled = [event["data"]["scheduled_for"] for event in fired]
        self.assertEqual(len(scheduled), len(set(scheduled)))
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_coalesce_collapses_a_week_of_downtime(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "wake", self.clock() + DAY, interval_seconds=DAY,
            misfire_policy=MisfirePolicy.COALESCE,
        )
        self.clock.advance(7 * DAY + 300)  # the daemon was down a week
        results = self.process_due()
        self.assertEqual(len(results[0]["fires"]), 1)
        fired = self.fired_events(mission_id)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["data"]["coalesced"], 7)
        timer = self.store.get_timer(timer_id)
        self.assertGreater(timer["due_at"], self.clock())

    def test_skip_drops_missed_occurrences(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "wake", self.clock() + DAY, interval_seconds=DAY,
            misfire_policy=MisfirePolicy.SKIP,
        )
        self.clock.advance(3 * DAY)
        results = self.process_due()
        self.assertTrue(results[0]["skipped"])
        self.assertEqual(self.fired_events(mission_id), [])
        timer = self.store.get_timer(timer_id)
        self.assertGreater(timer["due_at"], self.clock())
        # on-time fires still happen under skip
        self.clock.advance(timer["due_at"] - self.clock() + 1)
        results = self.process_due()
        self.assertEqual(len(results[0]["fires"]), 1)
        self.assertEqual(self.store.get_timer(timer_id)["status"], "active")

    def test_catch_up_is_bounded(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + DAY, interval_seconds=DAY,
            misfire_policy=MisfirePolicy.CATCH_UP, catch_up_limit=5,
        )
        self.clock.advance(20 * DAY)  # 20 missed days
        results = self.process_due()
        self.assertEqual(len(results[0]["fires"]), 5)
        fired = self.fired_events(mission_id)
        self.assertEqual(len(fired), 5)
        # the fires carry the true scheduled times, oldest first
        scheduled = [event["data"]["scheduled_for"] for event in fired]
        self.assertEqual(scheduled, sorted(scheduled))
        # and the timer advanced past now — the remainder coalesced away
        timer = self.store.find_timer(mission_id, "wake")
        self.assertGreater(timer["due_at"], self.clock())

    def test_catch_up_hard_cap(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 3600, interval_seconds=3600,
            misfire_policy=MisfirePolicy.CATCH_UP, catch_up_limit=999999,
        )
        self.clock.advance(365 * DAY)
        results = self.process_due()
        self.assertLessEqual(len(results[0]["fires"]), 32)

    def test_on_time_fire_within_grace_is_not_a_misfire(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + DAY, interval_seconds=DAY,
            misfire_policy=MisfirePolicy.SKIP,
        )
        self.clock.advance(DAY + 30)  # 30s late, inside the grace window
        results = self.process_due()
        self.assertEqual(len(results[0]["fires"]), 1)


class TestKillRestartBoundaries(TimerCase):
    """Simulated crashes at every boundary of the claim→fire→advance
    protocol. The invariant: each scheduled occurrence fires at most once."""

    def reopen(self):
        """Simulate a daemon restart: close and reopen the store."""
        self.store.close()
        self.store = MissionStore(self.db_path, clock=self.clock)

    def test_crash_after_claim_before_fire(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        self.clock.advance(61)
        claims = self.store.claim_due_timers("daemon-a", lease_seconds=120)
        self.assertEqual(len(claims), 1)
        self.reopen()  # crash: the claim is orphaned
        # before the claim lease expires nobody else can claim
        self.assertEqual(self.store.claim_due_timers("daemon-b"), [])
        self.clock.advance(121)
        results = self.process_due(holder="daemon-b")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.fired_events(mission_id)), 1)

    def test_crash_after_fire_commit(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        self.clock.advance(61)
        self.process_due(holder="daemon-a")
        self.reopen()  # crash right after the fire transaction committed
        # restart processing finds nothing due — no duplicate fire
        self.assertEqual(self.process_due(holder="daemon-a"), [])
        self.assertEqual(len(self.fired_events(mission_id)), 1)

    def test_stale_holder_cannot_fire_after_takeover(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        self.clock.advance(61)
        stale = self.store.claim_due_timers("daemon-a", lease_seconds=100)
        self.clock.advance(101)  # daemon-a hangs; its claim lease expires
        fresh = self.store.claim_due_timers("daemon-b", lease_seconds=100)
        self.assertEqual(len(fresh), 1)
        # the hung daemon wakes up and tries to fire its stale claim while
        # daemon-b's claim is live — rejected by the holder check
        with self.assertRaises(StaleGenerationError):
            self.store.fire_timer(
                timer_id, stale[0]["generation"], holder="daemon-a"
            )
        self.store.fire_timer(
            timer_id, fresh[0]["generation"], holder="daemon-b"
        )
        # after the fire advanced the generation, the stale claim is dead
        # even once the fresh claim is gone
        with self.assertRaises(StaleGenerationError):
            self.store.fire_timer(
                timer_id, stale[0]["generation"], holder="daemon-a"
            )
        self.assertEqual(len(self.fired_events(mission_id)), 1)

    def test_restart_between_wake_and_session_does_not_refire(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        self.clock.advance(61)
        self.process_due()
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.READY
        )
        self.reopen()  # crash after wake, before the session started
        self.assertEqual(self.process_due(), [])  # timer already advanced
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        self.assertEqual(len(self.fired_events(mission_id)), 1)

    def test_crash_mid_session_then_resume(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        self.clock.advance(61)
        self.process_due()
        self.store.start_session(
            mission_id, "ses-1", "daemon-a", {}, lease_seconds=300
        )
        self.reopen()  # kill -9 mid-session
        # reconcile refuses while the session lease is live
        self.assertEqual(
            self.store.reconcile()["sessions_abandoned"], 0
        )
        self.clock.advance(301)
        report = self.store.reconcile()
        self.assertEqual(report["sessions_abandoned"], 1)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        # the retried session checkpoints normally; the ledger stays clean
        self.store.start_session(mission_id, "ses-2", "daemon-a", {})
        self.store.checkpoint_session(
            mission_id, "ses-2", "daemon-a", "recovered",
            MissionState.WAITING_TIMER,
        )
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_reschedule_with_stale_generation_rejected(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        generation = self.store.get_timer(timer_id)["generation"]
        self.store.reschedule_timer(
            timer_id, self.clock() + 600, expected_generation=generation
        )
        with self.assertRaises(StaleGenerationError):
            self.store.reschedule_timer(
                timer_id, self.clock() + 900, expected_generation=generation
            )


class TestTimerEffects(TimerCase):
    def test_wake_is_noop_for_paused_missions(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=3600
        )
        self.store.transition_mission(mission_id, MissionState.PAUSED)
        self.clock.advance(61)
        self.process_due()
        self.assertEqual(
            self.store.get_mission(mission_id)["status"],
            MissionState.PAUSED,
        )
        # the timer advanced normally — resume picks up the next occurrence
        self.assertGreater(
            self.store.get_timer(timer_id)["due_at"], self.clock()
        )

    def test_timer_dies_with_terminal_mission(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "cleanup", self.clock() + 60, interval_seconds=3600
        )
        self.store.transition_mission(mission_id, MissionState.CANCELLED)
        self.clock.advance(61)
        self.process_due()
        self.assertEqual(
            self.store.get_timer(timer_id)["status"], "cancelled"
        )

    def test_outbox_effect_enqueues_once(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(
            mission_id, "notify", self.clock() + 60,
            payload={
                "effect": "outbox", "kind": "channel_notify",
                "payload": {"text": "ping"}, "dedupe_key": "ping:1",
            },
        )
        self.clock.advance(61)
        self.process_due()
        outbox = self.store.list_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["dedupe_key"], "ping:1")

    def test_unknown_effect_fails_closed(self):
        mission_id = self.make_waiting_mission()
        timer_id = self.store.create_timer(
            mission_id, "strange", self.clock() + 60,
            payload={"effect": "launch_rockets"},
        )
        self.clock.advance(61)
        claims = self.store.claim_due_timers("d")
        with self.assertRaises(KernelError):
            self.store.fire_timer(timer_id, claims[0]["generation"], holder="d")
        # the failed fire rolled back atomically: no fired event exists
        self.assertEqual(self.fired_events(mission_id), [])

    def test_duplicate_logical_key_rejected(self):
        mission_id = self.make_waiting_mission()
        self.store.create_timer(mission_id, "wake", self.clock() + 60)
        with self.assertRaises(KernelError):
            self.store.create_timer(mission_id, "wake", self.clock() + 90)


if __name__ == "__main__":
    unittest.main()
