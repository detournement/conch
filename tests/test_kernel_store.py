"""Mission kernel store gates (Swarm Phase 1).

Proven here: single-transaction mutation discipline (optimistic versions +
event append + projection update + budget ops + outbox insert), immutable
hash-chained events with fail-closed version handling, hard budget
enforcement (reserve/exceed/commit/release, child scopes as strict subsets),
origin/expiry/nonce approval rejection, external-action idempotency and
query-before-retry, transactional inbox/outbox with dedupe and
exactly-once-effect marking, replayable projections (rebuild from events ==
live), the backup/restore drill, epoch fencing, and crash reconciliation.
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from conch.kernel.model import (
    ApprovalError,
    BudgetExceededError,
    ConflictError,
    EVENT_SCHEMA_VERSION,
    KernelError,
    MissionState,
    model_completion_allowed,
    normalize_spec,
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


BASE_SPEC = {
    "goal": "keep the repo digest fresh",
    "success_criteria": ["a digest exists for every day"],
    "budgets": {"tokens": 10000, "sessions": 10},
    "cadence_seconds": 3600,
}


class KernelCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "kernel" / "kernel.db"
        self.clock = FakeClock()
        self.store = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(self.store.close)

    def make_mission(self, **overrides):
        spec = dict(BASE_SPEC)
        spec.update(overrides)
        return self.store.create_mission(spec)


class TestSpecNormalization(unittest.TestCase):
    def test_goal_required(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "  "})

    def test_unknown_fields_fail_closed(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "surprise": 1})

    def test_dry_run_defaults_true(self):
        spec = normalize_spec({"goal": "x"})
        self.assertTrue(spec["dry_run"])

    def test_budgets_must_be_integer_units(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "budgets": {"usd": 1.5}})
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "budgets": {"tokens": -1}})
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "budgets": {"live": True}})

    def test_scheduled_prompt_requires_prompt(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "kind": "scheduled_prompt"})

    def test_session_bounds_clamped_to_ceilings(self):
        spec = normalize_spec({"goal": "x", "session_wall_seconds": 999999})
        self.assertEqual(spec["session_wall_seconds"], 3600)

    def test_misfire_policy_validated(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "misfire_policy": "improvise"})

    def test_allow_model_completion_defaults_off_for_cadence_specs(self):
        # Recurring schedule + no success criteria = cadence mission: the
        # model may not self-complete it.
        self.assertFalse(
            normalize_spec({"goal": "daily digest"})[
                "allow_model_completion"
            ]
        )
        # Success criteria, run-once, or no recurrence carry completion
        # semantics — default stays permissive.
        self.assertTrue(
            normalize_spec({"goal": "x", "success_criteria": ["done"]})[
                "allow_model_completion"
            ]
        )
        self.assertTrue(
            normalize_spec({"goal": "x", "run_once": True})[
                "allow_model_completion"
            ]
        )
        self.assertTrue(
            normalize_spec({"goal": "x", "cadence_seconds": 0})[
                "allow_model_completion"
            ]
        )
        # Explicit values win in both directions.
        self.assertTrue(
            normalize_spec(
                {"goal": "x", "allow_model_completion": True}
            )["allow_model_completion"]
        )
        self.assertFalse(
            normalize_spec({
                "goal": "x", "success_criteria": ["done"],
                "allow_model_completion": False,
            })["allow_model_completion"]
        )

    def test_allow_model_completion_must_be_boolean(self):
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "allow_model_completion": "yes"})
        with self.assertRaises(KernelError):
            normalize_spec({"goal": "x", "allow_model_completion": 1})

    def test_model_completion_allowed_handles_legacy_specs(self):
        # Stored specs normalized before the field existed have no key —
        # they get the same cadence-style default as new specs.
        self.assertFalse(model_completion_allowed(
            {"goal": "d", "cadence_seconds": 86400}
        ))
        self.assertTrue(model_completion_allowed(
            {"goal": "d", "cadence_seconds": 86400,
             "success_criteria": ["x"]}
        ))
        self.assertTrue(model_completion_allowed(
            {"goal": "d", "cadence_seconds": 86400,
             "allow_model_completion": True}
        ))

    def test_capitol_bind_scheduled_normalizes_and_gates(self):
        spec = normalize_spec({
            "goal": "x",
            "capitol": {"workflows": ["wf-1"], "bind_scheduled": True},
        })
        self.assertTrue(spec["capitol"]["bind_scheduled"])
        self.assertFalse(spec["capitol"]["allow_start"])
        # defaults off
        spec = normalize_spec(
            {"goal": "x", "capitol": {"workflows": ["wf-1"]}}
        )
        self.assertFalse(spec["capitol"]["bind_scheduled"])
        # discovery without an allowlist is meaningless — fail closed
        with self.assertRaises(KernelError):
            normalize_spec(
                {"goal": "x", "capitol": {"bind_scheduled": True}}
            )


class TestStoreBasics(KernelCase):
    def test_wal_mode_and_single_file_under_state_dir(self):
        conn = sqlite3.connect(str(self.db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")
        self.assertTrue(self.db_path.exists())

    def test_db_file_not_world_readable(self):
        mode = os.stat(self.db_path).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, f"kernel db mode {oct(mode)}")

    def test_single_writer_serializes_concurrent_mutations(self):
        mission_id = self.make_mission()
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    self.store.record_note(mission_id, f"note {n}-{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(n,)) for n in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        # 2 creation events + 80 notes, all committed
        self.assertEqual(self.store.event_count(mission_id), 82)
        self.assertTrue(self.store.verify_chain(mission_id) >= 82)


class TestMissionStateMachine(KernelCase):
    def test_lifecycle_and_versions(self):
        mission_id = self.make_mission()
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.DRAFT)
        self.assertEqual(mission["version"], 1)
        self.store.transition_mission(mission_id, MissionState.READY)
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        self.assertEqual(mission["version"], 2)

    def test_illegal_transition_fails_closed(self):
        mission_id = self.make_mission()
        with self.assertRaises(KernelError):
            self.store.transition_mission(
                mission_id, MissionState.SUCCEEDED
            )
        # nothing changed
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.DRAFT
        )

    def test_unknown_state_fails_closed(self):
        mission_id = self.make_mission()
        with self.assertRaises(KernelError):
            self.store.transition_mission(mission_id, "warp_speed")

    def test_optimistic_version_conflict(self):
        mission_id = self.make_mission()
        with self.assertRaises(ConflictError):
            self.store.transition_mission(
                mission_id, MissionState.READY, expected_version=99
            )
        self.store.transition_mission(
            mission_id, MissionState.READY, expected_version=1
        )

    def test_terminal_states_are_final(self):
        mission_id = self.make_mission()
        self.store.transition_mission(mission_id, MissionState.CANCELLED)
        for target in MissionState.ALL:
            with self.assertRaises(KernelError):
                self.store.transition_mission(mission_id, target)

    def test_every_transition_is_persisted_as_event(self):
        mission_id = self.make_mission()
        self.store.transition_mission(mission_id, MissionState.READY)
        kinds = [
            event["kind"]
            for event in self.store.event_tail(mission_id, limit=10)
        ]
        self.assertIn("mission_created", kinds)
        self.assertIn("mission_transitioned", kinds)


class TestEventChain(KernelCase):
    def test_chain_verifies(self):
        mission_id = self.make_mission()
        self.store.transition_mission(mission_id, MissionState.READY)
        self.assertGreaterEqual(self.store.verify_chain(mission_id), 3)

    def test_tampering_detected(self):
        mission_id = self.make_mission()
        self.store.record_note(mission_id, "original fact")
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "UPDATE mission_events SET data=replace(data, 'original',"
            " 'rewritten') WHERE kind='mission_note'"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(KernelError):
            self.store.verify_chain(mission_id)

    def test_unknown_event_version_fails_closed(self):
        mission_id = self.make_mission()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO mission_events(mission_id, kind, data,"
            " schema_version, created_at, prev_hash, hash)"
            " VALUES (?, 'mission_note', '{}', ?, 1, 'x', 'y')",
            (mission_id, EVENT_SCHEMA_VERSION + 1),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(KernelError):
            self.store.event_tail(mission_id, limit=100)
        with self.assertRaises(KernelError):
            self.store.verify_chain(mission_id)
        with self.assertRaises(KernelError):
            self.store.replay_projections()


class TestBudgets(KernelCase):
    def scope_of(self, mission_id):
        return self.store.get_mission(mission_id)["root_scope_id"]

    def test_reserve_within_cap(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        self.store.reserve_budget(
            mission_id, scope, {"tokens": 4000}, "res-1"
        )
        status = self.store.budget_status(scope)
        self.assertEqual(status["tokens"]["reserved"], 4000)
        self.assertEqual(status["tokens"]["available"], 6000)

    def test_reserve_exceeding_cap_fails_and_changes_nothing(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        events_before = self.store.event_count(mission_id)
        with self.assertRaises(BudgetExceededError):
            self.store.reserve_budget(
                mission_id, scope, {"tokens": 10001}, "res-1"
            )
        self.assertEqual(self.store.event_count(mission_id), events_before)
        status = self.store.budget_status(scope)
        self.assertEqual(status["tokens"]["reserved"], 0)

    def test_multi_line_reserve_is_atomic(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        with self.assertRaises(BudgetExceededError):
            self.store.reserve_budget(
                mission_id, scope, {"tokens": 100, "sessions": 11}, "res-1"
            )
        status = self.store.budget_status(scope)
        self.assertEqual(status["tokens"]["reserved"], 0)
        self.assertEqual(status["sessions"]["reserved"], 0)

    def test_commit_within_reservation_releases_remainder(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        self.store.reserve_budget(
            mission_id, scope, {"tokens": 4000}, "res-1"
        )
        self.store.commit_budget(
            mission_id, scope, "res-1", {"tokens": 2500}
        )
        status = self.store.budget_status(scope)
        self.assertEqual(status["tokens"]["committed"], 2500)
        self.assertEqual(status["tokens"]["reserved"], 0)
        self.assertEqual(status["tokens"]["available"], 7500)

    def test_commit_above_reservation_fails(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        self.store.reserve_budget(
            mission_id, scope, {"tokens": 1000}, "res-1"
        )
        with self.assertRaises(BudgetExceededError):
            self.store.commit_budget(
                mission_id, scope, "res-1", {"tokens": 1001}
            )

    def test_release_returns_availability(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        self.store.reserve_budget(
            mission_id, scope, {"tokens": 9000}, "res-1"
        )
        self.store.release_budget(mission_id, scope, "res-1")
        status = self.store.budget_status(scope)
        self.assertEqual(status["tokens"]["available"], 10000)
        with self.assertRaises(KernelError):
            self.store.release_budget(mission_id, scope, "res-1")

    def test_duplicate_reservation_id_rejected(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        self.store.reserve_budget(mission_id, scope, {"tokens": 1}, "res-1")
        with self.assertRaises(KernelError):
            self.store.reserve_budget(
                mission_id, scope, {"tokens": 1}, "res-1"
            )

    def test_child_scope_is_strict_subset(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        with self.assertRaises(BudgetExceededError):
            self.store.create_child_scope(
                mission_id, scope, {"tokens": 10001}
            )
        child = self.store.create_child_scope(
            mission_id, scope, {"tokens": 6000}
        )
        # parent holds the child's cap as a reservation
        parent_status = self.store.budget_status(scope)
        self.assertEqual(parent_status["tokens"]["reserved"], 6000)
        # the child cannot exceed its own cap
        with self.assertRaises(BudgetExceededError):
            self.store.reserve_budget(
                mission_id, child, {"tokens": 6001}, "res-c"
            )
        # a second child cannot take more than what remains
        with self.assertRaises(BudgetExceededError):
            self.store.create_child_scope(
                mission_id, scope, {"tokens": 4001}
            )

    def test_close_child_commits_actuals_to_parent(self):
        mission_id = self.make_mission()
        scope = self.scope_of(mission_id)
        child = self.store.create_child_scope(
            mission_id, scope, {"tokens": 6000}
        )
        self.store.reserve_budget(
            mission_id, child, {"tokens": 3000}, "res-c"
        )
        self.store.commit_budget(mission_id, child, "res-c", {"tokens": 2000})
        actuals = self.store.close_child_scope(mission_id, child)
        self.assertEqual(actuals, {"tokens": 2000})
        parent_status = self.store.budget_status(scope)
        self.assertEqual(parent_status["tokens"]["committed"], 2000)
        self.assertEqual(parent_status["tokens"]["reserved"], 0)
        self.assertEqual(parent_status["tokens"]["available"], 8000)
        with self.assertRaises(KernelError):
            self.store.close_child_scope(mission_id, child)


class TestApprovals(KernelCase):
    def request(self, mission_id, **kwargs):
        defaults = {
            "origin_channel": "slack", "origin_thread": "t1",
            "origin_sender": "U123", "ttl_seconds": 600.0,
        }
        defaults.update(kwargs)
        return self.store.request_approval(
            mission_id, "publish_listing", {"price_cents": 4200}, **defaults
        )

    def test_decide_with_bound_origin(self):
        mission_id = self.make_mission()
        grant = self.request(mission_id)
        result = self.store.decide_approval(
            grant["approval_id"], "approve", nonce=grant["nonce"],
            origin_channel="slack", origin_thread="t1",
            origin_sender="U123", decided_by="U123",
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["args_hash"], grant["args_hash"])

    def test_wrong_origin_rejected(self):
        mission_id = self.make_mission()
        grant = self.request(mission_id)
        with self.assertRaises(ApprovalError):
            self.store.decide_approval(
                grant["approval_id"], "approve", nonce=grant["nonce"],
                origin_channel="slack", origin_thread="t1",
                origin_sender="U999",
            )
        record = self.store.get_approval(grant["approval_id"])
        self.assertEqual(record["status"], "pending")

    def test_expired_rejected_and_marked(self):
        mission_id = self.make_mission()
        grant = self.request(mission_id, ttl_seconds=100.0)
        self.clock.advance(101)
        with self.assertRaises(ApprovalError):
            self.store.decide_approval(
                grant["approval_id"], "approve", nonce=grant["nonce"],
                origin_channel="slack", origin_thread="t1",
                origin_sender="U123",
            )
        record = self.store.get_approval(grant["approval_id"])
        self.assertEqual(record["status"], "expired")

    def test_replayed_nonce_rejected(self):
        mission_id = self.make_mission()
        grant = self.request(mission_id)
        self.store.decide_approval(
            grant["approval_id"], "approve", nonce=grant["nonce"],
            origin_channel="slack", origin_thread="t1",
            origin_sender="U123",
        )
        with self.assertRaises(ApprovalError):
            self.store.decide_approval(
                grant["approval_id"], "approve", nonce=grant["nonce"],
                origin_channel="slack", origin_thread="t1",
                origin_sender="U123",
            )

    def test_wrong_nonce_rejected(self):
        mission_id = self.make_mission()
        grant = self.request(mission_id)
        with self.assertRaises(ApprovalError):
            self.store.decide_approval(
                grant["approval_id"], "approve", nonce="0" * 16,
                origin_channel="slack", origin_thread="t1",
                origin_sender="U123",
            )

    def test_local_operator_can_decide_channel_bound_approval(self):
        mission_id = self.make_mission()
        grant = self.request(mission_id)
        result = self.store.decide_approval(
            grant["approval_id"], "deny", nonce=grant["nonce"],
            origin_channel="local", decided_by="operator",
        )
        self.assertEqual(result["status"], "denied")
        record = self.store.get_approval(grant["approval_id"])
        self.assertTrue(record["decision_origin"].startswith("local"))

    def test_expiry_sweep(self):
        mission_id = self.make_mission()
        self.request(mission_id, ttl_seconds=50.0)
        self.request(mission_id, ttl_seconds=5000.0)
        self.clock.advance(60)
        expired = self.store.expire_approvals()
        self.assertEqual(expired, 1)
        self.assertEqual(len(self.store.pending_approvals()), 1)

    def test_notification_enqueued_with_request(self):
        mission_id = self.make_mission()
        self.store.request_approval(
            mission_id, "publish", {"x": 1},
            notify_payload={"text": "approval needed"},
        )
        outbox = self.store.list_outbox(status="pending")
        self.assertEqual(len(outbox), 1)


class TestExternalActions(KernelCase):
    def test_idempotency_key_dedupes(self):
        mission_id = self.make_mission()
        first = self.store.record_action(
            mission_id, "communicate", "notify:day1"
        )
        self.assertFalse(first["duplicate"])
        events_before = self.store.event_count(mission_id)
        second = self.store.record_action(
            mission_id, "communicate", "notify:day1"
        )
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["action_id"], first["action_id"])
        self.assertEqual(self.store.event_count(mission_id), events_before)

    def test_unknown_action_class_rejected(self):
        mission_id = self.make_mission()
        with self.assertRaises(KernelError):
            self.store.record_action(mission_id, "teleport", "k1")

    def test_unknown_outcome_query_before_retry(self):
        mission_id = self.make_mission()
        action = self.store.record_action(
            mission_id, "publish", "publish:item1"
        )
        self.store.resolve_action(action["action_id"], "unknown")
        # after querying the external system, the truth can be recorded
        self.store.resolve_action(action["action_id"], "committed")
        with self.assertRaises(KernelError):
            self.store.resolve_action(action["action_id"], "failed")


class TestInboxOutbox(KernelCase):
    def test_inbox_idempotent(self):
        mission_id = self.make_mission()
        first = self.store.receive_inbox(
            "slack", "msg-1", {"text": "hello"}, mission_id
        )
        second = self.store.receive_inbox(
            "slack", "msg-1", {"text": "hello"}, mission_id
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.store.mark_inbox_processed("msg-1")
        with self.assertRaises(KernelError):
            self.store.mark_inbox_processed("msg-1")

    def test_outbox_dedupe_key(self):
        mission_id = self.make_mission()
        self.assertTrue(self.store.enqueue_outbox(
            "channel_notify", {"text": "hi"}, "note:1", mission_id
        ))
        self.assertFalse(self.store.enqueue_outbox(
            "channel_notify", {"text": "hi again"}, "note:1", mission_id
        ))
        self.assertEqual(len(self.store.list_outbox()), 1)

    def test_exactly_once_effect_under_redelivery(self):
        """A crash between delivery and the delivered-mark causes redelivery;
        the mark is atomic and the dedupe key makes the effect idempotent."""
        mission_id = self.make_mission()
        self.store.enqueue_outbox(
            "channel_notify", {"text": "hi"}, "note:1", mission_id
        )
        effects = []

        def deliver(item):
            effects.append(item["dedupe_key"])

        claimed = self.store.claim_deliverable_outbox()
        self.assertEqual(len(claimed), 1)
        deliver(claimed[0])
        # crash here: delivered-mark never happened; backoff elapses
        self.clock.advance(3600)
        reclaimed = self.store.claim_deliverable_outbox()
        self.assertEqual(len(reclaimed), 1)
        # the receiver dedupes on the key — the effect list is deduplicated
        if reclaimed[0]["dedupe_key"] not in effects:
            deliver(reclaimed[0])
        self.assertTrue(
            self.store.mark_outbox_delivered(
                reclaimed[0]["outbox_id"], "test"
            )
        )
        # a late duplicate mark loses the race and must not re-effect
        self.assertFalse(
            self.store.mark_outbox_delivered(
                reclaimed[0]["outbox_id"], "test"
            )
        )
        self.assertEqual(effects, ["note:1"])
        delivered = self.store.list_outbox(status="delivered")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["attempts"], 2)

    def test_undelivered_stays_pending_with_backoff(self):
        mission_id = self.make_mission()
        self.store.enqueue_outbox(
            "channel_notify", {"text": "hi"}, "note:1", mission_id
        )
        claimed = self.store.claim_deliverable_outbox()
        self.store.mark_outbox_failed(
            claimed[0]["outbox_id"], "channel down"
        )
        # not yet due for retry
        self.assertEqual(self.store.claim_deliverable_outbox(), [])
        self.clock.advance(3600)
        self.assertEqual(len(self.store.claim_deliverable_outbox()), 1)


class TestArtifactsAndBindings(KernelCase):
    def test_inline_artifact_digest(self):
        mission_id = self.make_mission()
        artifact_id = self.store.record_artifact(
            mission_id, "digest.md", content="# Day 1\nAll quiet."
        )
        rows = self.store._read_conn().execute(
            "SELECT digest, size FROM artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        self.assertEqual(len(rows[0]), 64)
        self.assertGreater(rows[1], 0)

    def test_oversized_inline_artifact_rejected(self):
        mission_id = self.make_mission()
        with self.assertRaises(KernelError):
            self.store.record_artifact(
                mission_id, "huge.bin", content="x" * 70000
            )

    def test_external_artifact_requires_digest_and_size(self):
        mission_id = self.make_mission()
        with self.assertRaises(KernelError):
            self.store.record_artifact(mission_id, "blob")
        self.store.record_artifact(
            mission_id, "blob", digest="a" * 64, size=123
        )

    def test_resource_binding(self):
        mission_id = self.make_mission()
        self.store.record_binding(
            mission_id, "capitol_workflow", {"workflow_id": "wf-1"}
        )
        row = self.store._read_conn().execute(
            "SELECT kind FROM resource_bindings WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
        self.assertEqual(row[0], "capitol_workflow")


class TestSessionFlows(KernelCase):
    def ready_mission(self, **overrides):
        mission_id = self.make_mission(**overrides)
        self.store.transition_mission(mission_id, MissionState.READY)
        return mission_id

    def test_start_session_atomic(self):
        mission_id = self.ready_mission()
        result = self.store.start_session(
            mission_id, "ses-1", "daemon-a", {"tokens": 5000, "sessions": 1}
        )
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.ACTIVE)
        scope = result["scope_id"]
        self.assertEqual(
            self.store.budget_status(scope)["tokens"]["reserved"], 5000
        )
        self.assertIsNotNone(
            self.store.get_lease("mission_session", mission_id)
        )

    def test_start_session_insufficient_budget_changes_nothing(self):
        mission_id = self.ready_mission()
        with self.assertRaises(BudgetExceededError):
            self.store.start_session(
                mission_id, "ses-1", "daemon-a", {"tokens": 99999}
            )
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        self.assertIsNone(
            self.store.get_lease("mission_session", mission_id)
        )

    def test_start_session_requires_ready(self):
        mission_id = self.make_mission()
        with self.assertRaises(KernelError):
            self.store.start_session(mission_id, "ses-1", "d", {})

    def test_start_session_blocked_by_stop(self):
        mission_id = self.ready_mission()
        self.store.set_stop(mission_id, True)
        with self.assertRaises(KernelError):
            self.store.start_session(mission_id, "ses-1", "d", {})

    def test_start_session_lease_exclusion(self):
        mission_id = self.ready_mission()
        self.store.start_session(mission_id, "ses-1", "daemon-a", {})
        # mission is active; another start fails on state before lease
        with self.assertRaises(KernelError):
            self.store.start_session(mission_id, "ses-2", "daemon-b", {})

    def test_checkpoint_commits_budgets_and_transitions(self):
        mission_id = self.ready_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 3600, interval_seconds=3600
        )
        self.store.start_session(
            mission_id, "ses-1", "daemon-a", {"tokens": 5000, "sessions": 1}
        )
        result = self.store.checkpoint_session(
            mission_id, "ses-1", "daemon-a", "did the work",
            MissionState.WAITING_TIMER,
            actuals={"tokens": 1200, "sessions": 1},
            next_wake_at=self.clock() + 7200,
            notify_payload={"text": "done"},
        )
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)
        self.assertEqual(mission["runs"], 1)
        scope = mission["root_scope_id"]
        status = self.store.budget_status(scope)
        self.assertEqual(status["tokens"]["committed"], 1200)
        self.assertEqual(status["tokens"]["reserved"], 0)
        self.assertEqual(status["sessions"]["committed"], 1)
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["due_at"], self.clock() + 7200)
        self.assertEqual(len(self.store.list_outbox(status="pending")), 1)
        self.assertIsNone(
            self.store.get_lease("mission_session", mission_id)
        )
        checkpoint = self.store.latest_checkpoint(mission_id)
        self.assertEqual(checkpoint["checkpoint_id"], result["checkpoint_id"])

    def test_checkpoint_requires_live_lease(self):
        mission_id = self.ready_mission()
        self.store.start_session(
            mission_id, "ses-1", "daemon-a", {}, lease_seconds=100
        )
        self.clock.advance(101)
        with self.assertRaises(KernelError):
            self.store.checkpoint_session(
                mission_id, "ses-1", "daemon-a", "late",
                MissionState.WAITING_TIMER,
            )
        with self.assertRaises(KernelError):
            self.store.checkpoint_session(
                mission_id, "ses-1", "daemon-b", "thief",
                MissionState.WAITING_TIMER,
            )

    def test_terminal_checkpoint_cancels_timers(self):
        mission_id = self.ready_mission()
        self.store.create_timer(
            mission_id, "wake", self.clock() + 3600, interval_seconds=3600
        )
        self.store.start_session(mission_id, "ses-1", "daemon-a", {})
        self.store.checkpoint_session(
            mission_id, "ses-1", "daemon-a", "all criteria met",
            MissionState.SUCCEEDED,
        )
        timer = self.store.find_timer(mission_id, "wake")
        self.assertEqual(timer["status"], "cancelled")

    def test_abandon_session_releases_and_retries(self):
        mission_id = self.ready_mission()
        self.store.start_session(
            mission_id, "ses-1", "daemon-a", {"tokens": 5000},
            lease_seconds=100,
        )
        # a live lease refuses abandonment
        self.assertFalse(self.store.abandon_session(mission_id, "ses-1"))
        self.clock.advance(101)
        self.assertTrue(self.store.abandon_session(mission_id, "ses-1"))
        mission = self.store.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.READY)
        scope = mission["root_scope_id"]
        self.assertEqual(
            self.store.budget_status(scope)["tokens"]["reserved"], 0
        )

    def test_reconcile_abandons_expired_sessions(self):
        mission_id = self.ready_mission()
        self.store.start_session(
            mission_id, "ses-1", "daemon-a", {"tokens": 100},
            lease_seconds=50,
        )
        self.clock.advance(51)
        report = self.store.reconcile()
        self.assertEqual(report["sessions_abandoned"], 1)
        self.assertEqual(
            self.store.get_mission(mission_id)["status"], MissionState.READY
        )


class TestReplayAndIntegrity(KernelCase):
    def run_workload(self):
        mission_id = self.make_mission()
        self.store.transition_mission(mission_id, MissionState.READY)
        self.store.record_plan(mission_id, {"steps": ["a", "b"]})
        task_id = self.store.create_task(mission_id, "first task")
        attempt_id = self.store.start_attempt(task_id)
        self.store.finish_attempt(attempt_id, "succeeded")
        self.store.transition_task(task_id, "done")
        self.store.create_timer(
            mission_id, "wake", self.clock() + 60, interval_seconds=60
        )
        self.store.start_session(
            mission_id, "ses-1", "daemon-a", {"tokens": 2000, "sessions": 1}
        )
        self.store.checkpoint_session(
            mission_id, "ses-1", "daemon-a", "session one",
            MissionState.WAITING_TIMER,
            actuals={"tokens": 900, "sessions": 1},
            next_wake_at=self.clock() + 120,
            notify_payload={"text": "session one done"},
        )
        grant = self.store.request_approval(
            mission_id, "publish", {"item": 1}, ttl_seconds=1000
        )
        self.store.decide_approval(
            grant["approval_id"], "approve", nonce=grant["nonce"],
            origin_channel="local",
        )
        action = self.store.record_action(mission_id, "publish", "pub:1")
        self.store.resolve_action(action["action_id"], "committed")
        self.store.record_artifact(mission_id, "note.md", content="hello")
        self.store.record_binding(mission_id, "repo", {"path": "/x"})
        self.store.receive_inbox("slack", "in-1", {"text": "hi"}, mission_id)
        self.store.mark_inbox_processed("in-1")
        return mission_id

    def test_replay_matches_live_after_workload(self):
        mission_id = self.run_workload()
        # operational churn that must NOT disturb replayed projections
        timer = self.store.find_timer(mission_id, "wake")
        self.clock.advance(3600)
        self.store.claim_due_timers("daemon-a")
        self.store.fire_timer(
            timer["timer_id"],
            self.store.find_timer(mission_id, "wake")["generation"],
            holder="daemon-a",
        )
        self.store.claim_deliverable_outbox()
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)
        report = self.store.verify_integrity()
        self.assertEqual(report["replay"], "match")
        self.assertGreater(report["events_verified"], 15)

    def test_backup_restore_drill(self):
        """Snapshot, keep working, corrupt the live db, restore, reconcile."""
        mission_id = self.run_workload()
        snapshot = Path(self._tmp.name) / "backups" / "kernel.snap.db"
        self.store.backup(snapshot)
        events_at_snapshot = self.store.event_count()
        # post-snapshot work that the restore intentionally loses
        self.store.record_note(mission_id, "will be lost by restore")
        self.store.close()
        # corrupt the live database beyond repair
        with open(self.db_path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"\x00" * 512)
        with self.assertRaises(Exception):
            broken = MissionStore(self.db_path, clock=self.clock)
            try:
                broken.verify_integrity()
            finally:
                broken.close()
        # restore the snapshot and verify full integrity + clean reconcile
        MissionStore.restore(snapshot, self.db_path)
        restored = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(restored.close)
        report = restored.verify_integrity()
        self.assertEqual(report["replay"], "match")
        self.assertEqual(restored.event_count(), events_at_snapshot)
        reconcile = restored.reconcile()
        self.assertEqual(reconcile["sessions_abandoned"], 0)
        mission = restored.get_mission(mission_id)
        self.assertEqual(mission["status"], MissionState.WAITING_TIMER)


class TestEpochFencing(KernelCase):
    def test_stale_epoch_cannot_write(self):
        mission_id = self.make_mission()
        old = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(old.close)
        old.adopt_epoch()
        new = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(new.close)
        new.adopt_epoch()
        with self.assertRaises(KernelError):
            old.record_note(mission_id, "from the dead daemon")
        new.record_note(mission_id, "from the live daemon")
        # the unfenced base store (no epoch adopted) still writes: it is the
        # direct shell path used only when no daemon holds the OS lock.
        self.store.record_note(mission_id, "direct")


if __name__ == "__main__":
    unittest.main()
