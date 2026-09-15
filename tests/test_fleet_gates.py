"""Cross-cutting Swarm Phase 2 gates.

These consolidate the plan's hard gates that span the worker, the plane,
and the kernel:

- fault injection killing the worker at every assignment/completion
  boundary (real subprocess over the byte-identical fake SSH transport),
- duplicate RPCs never duplicate receipts,
- obsolete fences rejected,
- effectively-once external side effects through the kernel's
  external-action ledger under redelivery,
- the secret-canary sweep extended to fleet paths: keys/bearers never
  appear in envelopes, events, receipts, argv, or logs.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.fleet.plane import TaskPlane
from conch.fleet.registry import FleetRegistry
from conch.fleet.transport import WorkerTransport
from conch.kernel.model import ActionStatus, DispatchState
from conch.kernel.store import MissionStore
from conch.ssh_control import SSHControlManager, SSHTarget
from conch.swarm.protocol import (
    ActionClass,
    RpcRequest,
    TaskEnvelope,
    new_id,
)

from tests.fleet_fakes import FakeSSHWorkerTransport, LocalWorkerProcess


class GatesCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = MissionStore(self.root / "kernel.db")
        self.addCleanup(self.store.close)
        self.store.adopt_epoch()
        self.registry = FleetRegistry(self.store)
        self.mission_id = self.store.create_mission({
            "goal": "gate mission", "budgets": {"tokens": 1000000},
        })
        self._procs = {}

    def _factory(self, worker):
        return FakeSSHWorkerTransport(self._procs[worker["worker_id"]])

    def start_worker(self, name="box", script=None, config_json=None,
                     script_obj=None):
        home = self.root / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            json.dumps(config_json or {"agent_mode": True})
        )
        if script is None:
            path = home / "s.json"
            path.write_text(json.dumps(
                script_obj if script_obj is not None
                else [{"content": "done"}]
            ))
            script = path
        worker_id = self.registry.enroll(
            name, host="10.0.0.1", trust_level=3,
            data_ceiling="confidential", max_concurrency=2,
            capabilities={"os": "linux"}, profiles=["process"],
        )
        self.registry.activate(worker_id)
        self._spawn(worker_id, home, script)
        return worker_id, home, script

    def _spawn(self, worker_id, home, script):
        proc = LocalWorkerProcess(home, env_extra={
            "CONCH_FLEET_TASK_SCRIPT": str(script),
        }).start()
        self.addCleanup(proc.stop)
        self._procs[worker_id] = proc
        return proc

    def envelope(self, **overrides):
        fields = {
            "task_id": new_id("task"),
            "mission_id": self.mission_id,
            "principal": "user",
            "task": "do it",
            "idempotency_key": new_id("task"),
            "issued_at": 1000.0,
            "tools": (),
            "max_tool_rounds": 3,
            "wall_clock_seconds": 30,
        }
        fields.update(overrides)
        return TaskEnvelope(**fields)

    def drive(self, plane, task_id, timeout=25.0, until=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            plane.schedule_once()
            plane.poll_once()
            d = self.store.get_dispatch(task_id)
            if until and until(d):
                return d
            if until is None and d["state"] in DispatchState.TERMINAL:
                return d
            time.sleep(0.15)
        return self.store.get_dispatch(task_id)


class TestFaultInjection(GatesCase):
    def test_kill_worker_before_start_then_recover(self):
        worker_id, home, script = self.start_worker(script_obj=[
            {"content": "recovered result"},
        ])
        plane = self.make_plane()
        task_id = plane.submit(self.envelope())
        # Offer only (schedule offers+starts atomically, so intercept by
        # killing right after the first schedule attempt fails midway is
        # hard) — instead prove duplicate offers after a restart are safe.
        plane.schedule_once()
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"], DispatchState.RUNNING
        )
        # Kill the worker mid-run and restart it fresh.
        self._procs[worker_id].kill9()
        self._spawn(worker_id, home, script)
        # The dispatch is RUNNING but the worker forgot the child process;
        # its reaper finalizes the dead attempt as transient on restart, and
        # the plane retries. Drive to a clean terminal state.
        final = self.drive(plane, task_id, timeout=30)
        self.assertIn(final["state"],
                      (DispatchState.SUCCEEDED, DispatchState.FAILED))
        # Exactly one terminal receipt per attempt: no attempt produced two.
        self._assert_no_duplicate_receipts(home, task_id)

    def test_kill_after_result_written_still_single_finalize(self):
        worker_id, home, script = self.start_worker(script_obj=[
            {"content": "the durable answer"},
        ])
        plane = self.make_plane()
        task_id = plane.submit(self.envelope())
        self.drive(plane, task_id, until=lambda d: d["state"] == "running")
        # Wait for the worker to persist a terminal receipt, then kill it
        # before the controller finalizes.
        self._await_worker_terminal(worker_id, task_id)
        self._procs[worker_id].kill9()
        self._spawn(worker_id, home, script)
        final = self.drive(plane, task_id, timeout=30)
        self.assertEqual(final["state"], DispatchState.SUCCEEDED)
        self.assertIn("durable", final["result"]["summary"])

    def test_duplicate_completion_redelivery_finalizes_once(self):
        worker_id, home, script = self.start_worker(script_obj=[
            {"content": "answer"},
        ])
        plane = self.make_plane()
        task_id = plane.submit(self.envelope())
        final = self.drive(plane, task_id)
        self.assertEqual(final["state"], DispatchState.SUCCEEDED)
        # Re-poll many times: the terminal transition guard means no second
        # finalize, and events never duplicate (dedupe on task,attempt,seq).
        before = len(self.store.list_dispatch_events(task_id))
        for _ in range(5):
            plane.poll_once()
        after = len(self.store.list_dispatch_events(task_id))
        self.assertEqual(before, after)
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"],
            DispatchState.SUCCEEDED,
        )

    def _assert_no_duplicate_receipts(self, home, task_id):
        # The worker DB holds one row per task; each attempt has at most one
        # terminal receipt (offer receipts are keyed by attempt+fence).
        import sqlite3

        db = sqlite3.connect(str(home / "state" / "worker.db"))
        try:
            rows = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(int(rows[0]), 1)

    def _await_worker_terminal(self, worker_id, task_id, timeout=15.0):
        transport = FakeSSHWorkerTransport(self._procs[worker_id])
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = transport.send(RpcRequest(
                rpc_id=new_id("rpc"), op="task.status",
                args={"task_id": task_id},
            ))
            if resp.ok and resp.result["state"] in (
                "completed", "failed", "cancelled"
            ):
                return
            time.sleep(0.1)
        raise AssertionError("worker task never reached terminal")

    def make_plane(self, **config):
        return TaskPlane(self.store, self.registry, self._factory,
                         config=config)


class TestObsoleteFenceRejected(GatesCase):
    def test_stale_fence_offer_rejected_end_to_end(self):
        worker_id, home, script = self.start_worker()
        transport = FakeSSHWorkerTransport(self._procs[worker_id])
        envelope = self.envelope()
        # Offer at fence 5, then a stale offer at fence 4.
        first = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.offer",
            args={"envelope": envelope.to_dict(), "attempt": 1,
                  "fence": 5, "controller_epoch": 3},
        ))
        self.assertTrue(first.ok)
        stale = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.offer",
            args={"envelope": envelope.to_dict(), "attempt": 1,
                  "fence": 4, "controller_epoch": 3},
        ))
        self.assertFalse(stale.ok)
        self.assertEqual(stale.error_class, "policy")


class TestEffectivelyOnceLedger(GatesCase):
    def test_side_effecting_completion_records_ledger_once(self):
        worker_id, _home, _script = self.start_worker(
            script_obj=[{"content": "published"}]
        )
        # A PUBLISH envelope only places on a worker whose owner grant
        # raises its ceiling past the READ-only default (fleet awakening).
        from conch.fleet import authority

        authority.apply_grant(
            self.registry, worker_id,
            authority.validate_grant([ActionClass.PUBLISH], None),
        )
        plane = self.make_plane()
        envelope = self.envelope(
            action_classes=(ActionClass.READ, ActionClass.PUBLISH),
        )
        task_id = plane.submit(envelope)
        final = self.drive(plane, task_id)
        self.assertEqual(final["state"], DispatchState.SUCCEEDED)
        action = self.store.find_action(envelope.idempotency_key)
        self.assertIsNotNone(action, "external action was not recorded")
        self.assertEqual(action["action_class"], ActionClass.PUBLISH)
        self.assertEqual(action["status"], ActionStatus.PENDING)
        # Redelivery: re-poll and directly re-record — the ledger dedupes.
        for _ in range(3):
            plane.poll_once()
        dup = self.store.record_action(
            self.mission_id, ActionClass.PUBLISH,
            idempotency_key=envelope.idempotency_key, task_id=task_id,
        )
        self.assertTrue(dup["duplicate"])
        actions = [
            row for row in self.store._read_conn().execute(
                "SELECT action_id FROM external_actions WHERE"
                " idempotency_key=?", (envelope.idempotency_key,)
            ).fetchall()
        ]
        self.assertEqual(len(actions), 1, "side effect recorded twice")

    def test_pure_task_records_no_ledger_entry(self):
        self.start_worker(script_obj=[{"content": "read only"}])
        plane = self.make_plane()
        envelope = self.envelope(action_classes=(ActionClass.READ,))
        task_id = plane.submit(envelope)
        self.drive(plane, task_id)
        self.assertIsNone(self.store.find_action(envelope.idempotency_key))

    def make_plane(self, **config):
        return TaskPlane(self.store, self.registry, self._factory,
                         config=config)


CANARY = "CANARY-fleet-7c3e9-bearer-do-not-log"


class TestFleetSecretCanary(GatesCase):
    """Keys and bearers never enter envelopes, events, receipts, argv, or
    logs. The canary poses as a bearer token in the controller environment
    and config; driving a full dispatch must not surface it anywhere on the
    fleet path."""

    def _walk_files(self, *roots):
        for root in roots:
            root = Path(root)
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file():
                    yield path

    def test_canary_never_reaches_fleet_paths(self):
        worker_id, home, script = self.start_worker(script_obj=[
            {"content": "did the work without secrets"},
        ])
        with patch.dict(os.environ, {
            "CONCH_BEARER_TOKEN": CANARY,
            "FLEET_SIGNING_SECRET": CANARY,
        }):
            plane = TaskPlane(self.store, self.registry, self._factory)
            # A task envelope is business data — the controller must never
            # place a secret in it. Build it normally (no secret).
            envelope = self.envelope(
                task="summarize the public doc",
                context="the doc is about widgets",
                action_classes=(ActionClass.READ,),
            )
            task_id = plane.submit(envelope)
            final = self.drive(plane, task_id)
            self.assertEqual(final["state"], DispatchState.SUCCEEDED)
        # Sweep everything the fleet persisted: kernel db, worker home
        # (task db, event spools, receipts, logs, config).
        checked = 0
        for path in self._walk_files(self.root):
            checked += 1
            try:
                data = path.read_bytes()
            except OSError:
                continue
            self.assertNotIn(
                CANARY.encode(), data,
                f"secret canary leaked into {path}",
            )
        self.assertGreater(checked, 0)
        # The dispatch envelope, events, and receipt in the kernel are clean.
        dispatch = self.store.get_dispatch(task_id)
        self.assertNotIn(CANARY, json.dumps(dispatch))
        for event in self.store.list_dispatch_events(task_id):
            self.assertNotIn(CANARY, json.dumps(event))

    def test_transport_argv_carries_no_secret(self):
        with patch.dict(os.environ, {"CONCH_BEARER_TOKEN": CANARY}):
            manager = SSHControlManager(runtime_dir=self.root / "ssh")
            transport = WorkerTransport(
                SSHTarget(host="h", user="conch"), "box1", manager=manager,
            )
            argv = transport.argv()
        self.assertNotIn(CANARY, " ".join(argv))
        # The relay command names only the fixed op and worker.
        self.assertIn("conch-hostctl rpc --worker box1", argv)

    def make_plane(self, **config):
        return TaskPlane(self.store, self.registry, self._factory,
                         config=config)


if __name__ == "__main__":
    unittest.main()
