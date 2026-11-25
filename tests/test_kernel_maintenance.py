"""Kernel maintenance: incremental journal verification (verified-through
checkpoint), inbox/outbox retention that replay reproduces, cheap status
counts, the inbox cursor, and reconcile finding long sessions."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from conch.kernel.model import KernelError, MissionState
from conch.kernel.store import (
    JOURNAL_CHECKPOINT_KEY,
    RETENTION_BATCH,
    MissionStore,
)

DAY = 86400.0

SPEC = {
    "goal": "maintenance fixture",
    "budgets": {"tokens": 10_000_000, "sessions": 100},
    "cadence_seconds": 3600,
}


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "kernel.db"
        self.clock = FakeClock()
        self.store = MissionStore(self.db_path, clock=self.clock)
        self.addCleanup(self.store.close)

    def raw(self, sql, params=()):
        conn = sqlite3.connect(str(self.db_path))
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def mission_with_notes(self, notes=5):
        mission_id = self.store.create_mission(dict(SPEC))
        for index in range(notes):
            self.store.record_note(mission_id, f"note {index}")
        return mission_id


class TestIncrementalVerification(StoreCase):
    def test_first_run_verifies_all_then_only_new_events(self):
        mission_id = self.mission_with_notes(5)
        total = self.store.event_count()
        first = self.store.verify_journal(incremental=True)
        self.assertEqual(first["mode"], "incremental")
        self.assertEqual(first["from_seq"], 0)
        self.assertEqual(first["events_verified"], total)
        self.assertEqual(first["missions"], 1)
        checkpoint = self.store.journal_checkpoint()
        self.assertEqual(checkpoint["seq"], total)

        again = self.store.verify_journal(incremental=True)
        self.assertEqual(again["events_verified"], 0)
        self.assertEqual(again["from_seq"], total)
        self.assertEqual(again["missions"], 1)

        self.store.record_note(mission_id, "after checkpoint")
        other = self.store.create_mission(dict(SPEC))
        self.store.record_note(other, "new chain")
        added = self.store.event_count() - total
        third = self.store.verify_journal(incremental=True)
        self.assertEqual(third["events_verified"], added)
        self.assertEqual(third["missions"], 2)
        self.assertEqual(
            self.store.journal_checkpoint()["seq"], self.store.event_count()
        )

    def test_tamper_after_checkpoint_detected_and_checkpoint_kept(self):
        mission_id = self.mission_with_notes(3)
        self.store.verify_journal(incremental=True)
        before = self.store.journal_checkpoint()
        self.store.record_note(mission_id, "fresh fact")
        self.raw(
            "UPDATE mission_events SET data=replace(data, 'fresh', 'stale')"
            " WHERE kind='mission_note' AND data LIKE '%fresh%'"
        )
        with self.assertRaisesRegex(KernelError, "hash mismatch"):
            self.store.verify_journal(incremental=True)
        self.assertEqual(self.store.journal_checkpoint(), before)

    def test_old_row_tamper_needs_full_verification(self):
        """Documented boundary: a non-head event before the checkpoint is
        not re-hashed at startup; the on-demand full checks catch it."""
        mission_id = self.mission_with_notes(4)
        self.store.verify_journal(incremental=True)
        self.store.record_note(mission_id, "head after checkpoint")
        self.raw(
            "UPDATE mission_events SET data=replace(data, 'note 1',"
            " 'note X') WHERE kind='mission_note' AND data LIKE '%note 1%'"
        )
        self.store.verify_journal(incremental=True)  # bounded check passes
        with self.assertRaisesRegex(KernelError, "hash mismatch"):
            self.store.verify_journal()  # default: every chain from genesis
        with self.assertRaisesRegex(KernelError, "hash mismatch"):
            self.store.verify_journal(full=True)
        with self.assertRaisesRegex(KernelError, "hash mismatch"):
            self.store.verify_chain()

    def test_rewritten_checkpoint_event_detected(self):
        self.mission_with_notes(3)
        self.store.verify_journal(incremental=True)
        seq = self.store.journal_checkpoint()["seq"]
        self.raw(
            "UPDATE mission_events SET hash=? WHERE seq=?", ("f" * 64, seq)
        )
        with self.assertRaisesRegex(KernelError, "checkpoint"):
            self.store.verify_journal(incremental=True)

    def test_truncated_journal_detected(self):
        mission_id = self.mission_with_notes(3)
        self.store.verify_journal(incremental=True)
        seq = self.store.journal_checkpoint()["seq"]
        self.raw("DELETE FROM mission_events WHERE seq>=?", (seq - 1,))
        with self.assertRaisesRegex(KernelError, "checkpoint"):
            self.store.verify_journal(incremental=True)
        self.assertTrue(mission_id)

    def test_deleted_chain_head_before_checkpoint_detected(self):
        first = self.mission_with_notes(3)
        self.mission_with_notes(3)  # second chain owns the checkpoint seq
        self.store.verify_journal(incremental=True)
        head = self.raw(
            "SELECT MAX(seq) FROM mission_events WHERE mission_id=?",
            (first,),
        )[0][0]
        self.raw("DELETE FROM mission_events WHERE seq=?", (head,))
        with self.assertRaisesRegex(KernelError, "chain heads"):
            self.store.verify_journal(incremental=True)

    def test_inserted_old_row_into_new_position_detected(self):
        """A row slipped in with an explicit seq below the checkpoint but
        after a chain's head changes that head."""
        first = self.mission_with_notes(2)
        self.mission_with_notes(3)
        self.store.verify_journal(incremental=True)
        row = self.raw(
            "SELECT mission_id, kind, data, schema_version, created_at,"
            " prev_hash, hash FROM mission_events WHERE mission_id=?"
            " ORDER BY seq DESC LIMIT 1", (first,),
        )[0]
        gap = self.raw("SELECT MIN(seq) FROM mission_events WHERE"
                       " mission_id!=?", (first,))[0][0]
        self.raw("DELETE FROM mission_events WHERE seq=?", (gap + 1,))
        self.raw(
            "INSERT INTO mission_events(seq, mission_id, kind, data,"
            " schema_version, created_at, prev_hash, hash)"
            " VALUES (?,?,?,?,?,?,?,?)", (gap + 1, *row),
        )
        with self.assertRaises(KernelError):
            self.store.verify_journal(incremental=True)

    def test_missing_checkpoint_falls_back_to_genesis(self):
        self.mission_with_notes(3)
        self.store.verify_journal(incremental=True)
        self.raw("DELETE FROM meta WHERE key=?", (JOURNAL_CHECKPOINT_KEY,))
        report = self.store.verify_journal(incremental=True)
        self.assertEqual(report["from_seq"], 0)
        self.assertEqual(report["events_verified"], self.store.event_count())

    def test_unreadable_checkpoint_fails_closed(self):
        self.mission_with_notes(1)
        self.store.verify_journal(incremental=True)
        self.raw("UPDATE meta SET value='{oops' WHERE key=?",
                 (JOURNAL_CHECKPOINT_KEY,))
        with self.assertRaisesRegex(KernelError, "unreadable"):
            self.store.verify_journal(incremental=True)

    def test_full_verification_advances_checkpoint(self):
        self.mission_with_notes(3)
        report = self.store.verify_journal(full=True)
        self.assertEqual(report["mode"], "full")
        self.assertEqual(report["replay"], "match")
        checkpoint = self.store.journal_checkpoint()
        self.assertEqual(checkpoint["seq"], self.store.event_count())
        self.assertEqual(
            self.store.verify_journal(incremental=True)["events_verified"], 0
        )

    def test_checkpoint_never_rewinds(self):
        self.mission_with_notes(3)
        self.store.verify_journal(incremental=True)
        seq = self.store.journal_checkpoint()["seq"]
        self.store._store_journal_checkpoint(1, "x" * 64, "y" * 64, 1)
        self.assertEqual(self.store.journal_checkpoint()["seq"], seq)

    def test_empty_journal(self):
        report = self.store.verify_journal(incremental=True)
        self.assertEqual(report["events_verified"], 0)
        self.assertIsNone(self.store.journal_checkpoint())


class TestRetention(StoreCase):
    def test_inbox_retention_rules_and_replay(self):
        mission_id = self.store.create_mission(dict(SPEC))
        self.store.receive_inbox("browser", "b-old", {"kind": "click"})
        self.store.receive_inbox("slack", "s-done", {"t": 1}, mission_id)
        self.store.mark_inbox_processed("s-done")
        self.store.receive_inbox("slack", "s-unread", {"t": 2}, mission_id)
        self.clock.advance(100 * DAY)
        self.store.receive_inbox("browser", "b-new", {"kind": "click"})
        events_before = self.store.event_count()

        result = self.store.apply_retention(90 * DAY)
        self.assertEqual(result["inbox_pruned"], 2)
        keys = {row[0] for row in self.raw(
            "SELECT idempotency_key FROM inbox")}
        self.assertEqual(keys, {"s-unread", "b-new"})
        # the journal keeps every original event (plus the pruning event)
        self.assertEqual(self.store.event_count(), events_before + 1)
        self.assertEqual(self.raw(
            "SELECT COUNT(*) FROM mission_events WHERE kind='inbox_received'"
        )[0][0], 4)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)
        self.assertEqual(
            self.store.verify_journal(full=True)["replay"], "match"
        )
        # idempotent: nothing more to prune, no new event
        self.assertEqual(
            self.store.apply_retention(90 * DAY)["inbox_pruned"], 0
        )
        self.assertEqual(self.store.event_count(), events_before + 1)

    def test_outbox_retention_only_delivered(self):
        self.store.enqueue_outbox("notify", {"text": "a"}, "k-a")
        self.store.enqueue_outbox("notify", {"text": "b"}, "k-b")
        self.store.enqueue_outbox("notify", {"text": "c"}, "k-c")
        rows = self.store.list_outbox()
        self.store.mark_outbox_delivered(rows[0]["outbox_id"], "test")
        self.store.mark_outbox_failed(rows[1]["outbox_id"], "nope",
                                      permanent=True)
        self.clock.advance(100 * DAY)
        result = self.store.apply_retention(90 * DAY)
        self.assertEqual(result["outbox_pruned"], 1)
        self.assertEqual(
            [row["dedupe_key"] for row in self.store.list_outbox()],
            ["k-b", "k-c"],
        )
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)
        self.store.verify_journal(full=True)

    def test_recent_rows_kept_and_zero_disables(self):
        self.store.receive_inbox("browser", "b1", {"kind": "click"})
        self.clock.advance(10 * DAY)
        self.assertEqual(
            self.store.apply_retention(90 * DAY)["inbox_pruned"], 0
        )
        self.clock.advance(100 * DAY)
        self.assertEqual(self.store.apply_retention(0)["inbox_pruned"], 0)
        self.assertEqual(len(self.store.list_inbox()), 1)

    def test_batches_bound_each_pass(self):
        for index in range(RETENTION_BATCH + 7):
            self.store.receive_inbox("browser", f"b{index}", {"i": index})
        self.clock.advance(100 * DAY)
        first = self.store.apply_retention(90 * DAY, max_batches=1)
        self.assertEqual(first["inbox_pruned"], RETENTION_BATCH)
        second = self.store.apply_retention(90 * DAY)
        self.assertEqual(second["inbox_pruned"], 7)
        self.assertEqual(len(self.store.list_inbox()), 0)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)

    def test_daemon_retention_days(self):
        from conch.kernel.daemon import DEFAULT_RETENTION_DAYS, EdgeDaemon

        root = Path(self._tmp.name)
        def days(config):
            return EdgeDaemon(config, kernel_dir=root / "k",
                              state_dir=root).retention_days()
        self.assertEqual(days({}), DEFAULT_RETENTION_DAYS)
        self.assertEqual(days({"kernel_retention_days": "0"}), 0)
        self.assertEqual(days({"kernel_retention_days": "-1"}), 0)
        self.assertEqual(days({"kernel_retention_days": "45"}), 45)
        # never shorter than the compile-suggestion window
        self.assertEqual(days({"kernel_retention_days": "10",
                               "compile_suggest_window_days": "60"}), 60)


class TestCheapReads(StoreCase):
    def test_count_outbox(self):
        self.assertEqual(self.store.count_outbox(status="pending"), 0)
        self.store.enqueue_outbox("notify", {"text": "a"}, "k-a")
        self.store.enqueue_outbox("notify", {"text": "b"}, "k-b")
        first = self.store.list_outbox()[0]["outbox_id"]
        self.store.mark_outbox_delivered(first, "test")
        self.assertEqual(self.store.count_outbox(status="pending"), 1)
        self.assertEqual(self.store.count_outbox(status="delivered"), 1)
        self.assertEqual(self.store.count_outbox(), 2)

    def test_list_inbox_after_id_pages_forward(self):
        ids = [
            self.store.receive_inbox("fw", f"k{index}", {"i": index})[
                "inbox_id"]
            for index in range(5)
        ]
        self.store.receive_inbox("other", "o1", {"i": 99})
        page = self.store.list_inbox(source="fw", limit=2, after_id=ids[0])
        self.assertEqual([row["inbox_id"] for row in page], ids[1:3])
        rest = self.store.list_inbox(source="fw", limit=10, after_id=ids[2])
        self.assertEqual([row["inbox_id"] for row in rest], ids[3:])
        self.assertEqual(
            self.store.list_inbox(source="fw", after_id=ids[-1]), []
        )
        # without a cursor: newest rows, chronological (unchanged)
        newest = self.store.list_inbox(source="fw", limit=2)
        self.assertEqual([row["inbox_id"] for row in newest], ids[3:])


class TestReconcileLongSession(StoreCase):
    def test_long_session_reservation_released(self):
        """Regression: reconcile looked for session_started only in the
        last 50 events; a session that wrote more than that was abandoned
        as "unknown" and its reservation leaked forever."""
        mission_id = self.store.create_mission(dict(SPEC))
        self.store.transition_mission(mission_id, MissionState.READY)
        result = self.store.start_session(
            mission_id, "ses-long", "daemon-a", {"tokens": 2_000_000},
            lease_seconds=50,
        )
        scope = result["scope_id"]
        for index in range(80):
            self.store.record_note(mission_id, f"tool round {index}")
        self.assertEqual(
            self.store.budget_status(scope)["tokens"]["reserved"], 2_000_000
        )
        self.clock.advance(51)
        report = self.store.reconcile()
        self.assertEqual(report["sessions_abandoned"], 1)
        self.assertEqual(
            self.store.budget_status(scope)["tokens"]["reserved"], 0
        )
        abandoned = self.store.last_event(mission_id, ("session_abandoned",))
        self.assertEqual(abandoned["data"]["session_id"], "ses-long")
        status = self.raw(
            "SELECT status FROM budget_reservations WHERE reservation_id=?",
            ("ses-long",),
        )[0][0]
        self.assertEqual(status, "released")


if __name__ == "__main__":
    unittest.main()
