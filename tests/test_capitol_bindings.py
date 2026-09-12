"""Kernel resource-binding lifecycle (Swarm Phase 3).

Bindings tie missions to external Capitol resources (org/agent/workflow/
version/context/run/session ids + idempotency keys) and carry the
supervision cursor. These tests prove the event-sourced lifecycle:
recorded and updated through journal events only, monotonic cursors,
terminal-state immutability, replay == live (including journals written
before the lifecycle columns existed), and the additive column migration
for pre-existing databases.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from conch.kernel.model import BindingStatus, KernelError
from conch.kernel.store import MissionStore


def _spec(goal="bind things"):
    return {"goal": goal, "budgets": {"sessions": 5}}


class BindingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MissionStore(Path(self._tmp.name) / "kernel.db")
        self.addCleanup(self.store.close)
        self.mission_id = self.store.create_mission(_spec())

    def _record_run_binding(self, **kwargs):
        resource = {
            "org_id": "org-1", "agent_id": "agent-1",
            "workflow_id": "wf-1", "workflow_version": "v2",
            "context_id": "ctx-1", "run_id": "run-1",
            "session_id": "sess-1", "idempotency_key": "msn:1:draft",
        }
        return self.store.record_binding(
            self.mission_id, "capitol_run", resource, **kwargs
        )

    def test_record_and_read_back(self):
        binding_id = self._record_run_binding(task_id="task-9")
        binding = self.store.get_binding(binding_id)
        self.assertEqual(binding["kind"], "capitol_run")
        self.assertEqual(binding["mission_id"], self.mission_id)
        self.assertEqual(binding["task_id"], "task-9")
        self.assertEqual(binding["status"], BindingStatus.ACTIVE)
        self.assertEqual(binding["cursor"], 0)
        self.assertEqual(binding["resource"]["run_id"], "run-1")
        self.assertEqual(binding["resource"]["idempotency_key"],
                         "msn:1:draft")
        self.assertEqual(binding["detail"], {})
        self.assertGreater(binding["updated_at"], 0)

    def test_update_cursor_and_status(self):
        binding_id = self._record_run_binding()
        self.store.update_binding(binding_id, cursor=7)
        self.store.update_binding(
            binding_id, status=BindingStatus.WAITING_HITL,
            detail={"request_id": "req-1"},
        )
        binding = self.store.get_binding(binding_id)
        self.assertEqual(binding["cursor"], 7)
        self.assertEqual(binding["status"], BindingStatus.WAITING_HITL)
        self.assertEqual(binding["detail"], {"request_id": "req-1"})

    def test_cursor_is_monotonic(self):
        binding_id = self._record_run_binding()
        self.store.update_binding(binding_id, cursor=10)
        with self.assertRaises(KernelError):
            self.store.update_binding(binding_id, cursor=9)
        # equal cursor is an idempotent no-op, not an error
        self.store.update_binding(binding_id, cursor=10)
        self.assertEqual(self.store.get_binding(binding_id)["cursor"], 10)

    def test_terminal_binding_is_immutable(self):
        binding_id = self._record_run_binding()
        self.store.update_binding(
            binding_id, status=BindingStatus.COMPLETED
        )
        with self.assertRaises(KernelError):
            self.store.update_binding(binding_id, cursor=99)

    def test_unknown_binding_and_bad_status_fail_closed(self):
        with self.assertRaises(KernelError):
            self.store.update_binding("bnd-none", cursor=1)
        with self.assertRaises(KernelError):
            self._record_run_binding(status="halfway")
        binding_id = self._record_run_binding()
        with self.assertRaises(KernelError):
            self.store.update_binding(binding_id, status="halfway")

    def test_noop_update_appends_no_event(self):
        binding_id = self._record_run_binding()
        before = self.store.event_count()
        self.store.update_binding(binding_id)  # nothing to change
        self.store.update_binding(binding_id, cursor=0)  # same cursor
        self.assertEqual(self.store.event_count(), before)

    def test_find_bindings_filters(self):
        first = self._record_run_binding()
        second = self.store.record_binding(
            self.mission_id, "capitol_artifact",
            {"artifact_id": "art-1"},
        )
        self.store.update_binding(
            first, status=BindingStatus.DEGRADED
        )
        runs = self.store.find_bindings(kind="capitol_run")
        self.assertEqual([b["binding_id"] for b in runs], [first])
        supervised = self.store.find_bindings(
            kind="capitol_run", statuses=BindingStatus.SUPERVISED
        )
        self.assertEqual(len(supervised), 1)
        degraded = self.store.find_bindings(status=BindingStatus.DEGRADED)
        self.assertEqual([b["binding_id"] for b in degraded], [first])
        mine = self.store.find_bindings(mission_id=self.mission_id)
        self.assertEqual(
            {b["binding_id"] for b in mine}, {first, second}
        )

    def test_replay_matches_live_after_lifecycle(self):
        binding_id = self._record_run_binding(task_id="task-1")
        self.store.update_binding(binding_id, cursor=3)
        self.store.update_binding(
            binding_id, status=BindingStatus.WAITING_HITL,
            detail={"request_id": "req-9", "node_id": "n-2"},
        )
        self.store.update_binding(
            binding_id, status=BindingStatus.ACTIVE, cursor=12,
            detail={},
        )
        self.store.update_binding(
            binding_id, status=BindingStatus.COMPLETED,
        )
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)
        report = self.store.verify_integrity()
        self.assertEqual(report["replay"], "match")

    def test_pre_lifecycle_events_replay_deterministically(self):
        """Journals written before the lifecycle columns existed replay to
        the same bytes the migrated live table holds."""
        mission_id = self.mission_id

        def fn(conn):
            # the exact event shape record_binding wrote before Phase 3
            self.store._append(conn, mission_id, "binding_recorded", {
                "binding_id": "bnd-0000000000000-0000000000000000",
                "binding_kind": "legacy",
                "resource": {"run_id": "old-run"},
            })
        self.store._mutate(fn)
        binding = self.store.get_binding(
            "bnd-0000000000000-0000000000000000"
        )
        self.assertEqual(binding["status"], BindingStatus.ACTIVE)
        self.assertEqual(binding["cursor"], 0)
        self.assertEqual(binding["updated_at"], 0)
        ok, detail = self.store.replay_matches_live()
        self.assertTrue(ok, detail)


class BindingColumnMigrationTests(unittest.TestCase):
    def test_old_database_gains_lifecycle_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "kernel.db"
            conn = sqlite3.connect(str(db_path))
            # the pre-Phase-3 table shape, with one legacy row
            conn.execute(
                "CREATE TABLE resource_bindings ("
                " binding_id TEXT PRIMARY KEY,"
                " mission_id TEXT NOT NULL,"
                " kind TEXT NOT NULL,"
                " resource TEXT NOT NULL,"
                " created_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO resource_bindings VALUES"
                " ('bnd-1', 'msn-1', 'capitol_run', '{}', 5.0)"
            )
            conn.commit()
            conn.close()
            store = MissionStore(db_path)
            try:
                binding = store.get_binding("bnd-1")
                self.assertEqual(binding["status"], BindingStatus.ACTIVE)
                self.assertEqual(binding["cursor"], 0)
                self.assertEqual(binding["task_id"], "")
                self.assertEqual(binding["detail"], {})
                self.assertEqual(binding["updated_at"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
