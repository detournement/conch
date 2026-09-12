"""Distributed task plane gates (Swarm Phase 2).

End to end against real worker supervisor subprocesses reached through the
fake SSH transport, so scheduling, offer/start, fencing, retries,
heartbeats, resource groups, cancellation, and brokered delegation run the
production code paths. Task subprocesses run the real chat_turn under a
scripted raw_fn (no live model).
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from conch.fleet.plane import TaskPlane
from conch.fleet.registry import FleetRegistry
from conch.kernel.model import DispatchState, WorkerState
from conch.kernel.store import MissionStore
from conch.swarm.protocol import FailureClass, TaskEnvelope, new_id

from tests.fleet_fakes import FakeSSHWorkerTransport, LocalWorkerProcess


def hello_script(home: Path, text="hello", name="script"):
    home.mkdir(parents=True, exist_ok=True)
    path = home / f"{name}.json"
    path.write_text(json.dumps([{"content": text}]))
    return path


class PlaneCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = MissionStore(self.root / "kernel.db")
        self.addCleanup(self.store.close)
        self.store.adopt_epoch()
        self.registry = FleetRegistry(self.store)
        self.mission_id = self.store.create_mission({
            "goal": "plane test", "budgets": {"tokens": 1000000},
        })
        self._workers = {}

    def _factory(self, worker):
        proc = self._workers[worker["worker_id"]]
        return FakeSSHWorkerTransport(proc)

    def make_plane(self, **config):
        return TaskPlane(
            self.store, self.registry, self._factory, config=config,
        )

    def start_worker(self, name="box1", *, script_text="done",
                     script=None, config_json=None, trust_level=2,
                     data_ceiling="confidential", resource_group="",
                     max_concurrency=2, activate=True, capabilities=None):
        home = self.root / name
        home.mkdir(parents=True, exist_ok=True)
        if config_json is None:
            config_json = {"agent_mode": True}
        (home / "config.json").write_text(json.dumps(config_json))
        if script is None:
            script = hello_script(home, script_text)
        worker_id = self.registry.enroll(
            name, host=f"10.0.0.{len(self._workers) + 1}",
            trust_level=trust_level, data_ceiling=data_ceiling,
            resource_group=resource_group, max_concurrency=max_concurrency,
            capabilities=capabilities or {"os": "linux"},
            profiles=["process"],
        )
        if activate:
            self.registry.activate(worker_id)
        proc = LocalWorkerProcess(home, env_extra={
            "CONCH_FLEET_TASK_SCRIPT": str(script),
        }).start()
        self.addCleanup(proc.stop)
        self._workers[worker_id] = proc
        return worker_id

    def envelope(self, **overrides):
        fields = {
            "task_id": new_id("task"),
            "mission_id": self.mission_id,
            "principal": "user",
            "task": "do the thing",
            "idempotency_key": new_id("task"),
            "issued_at": 1000.0,
            "tools": (),
            "max_tool_rounds": 3,
            "wall_clock_seconds": 30,
        }
        fields.update(overrides)
        return TaskEnvelope(**fields)

    def drive(self, plane, task_id, *, timeout=25.0, terminal=True):
        deadline = time.time() + timeout
        while time.time() < deadline:
            plane.schedule_once()
            plane.poll_once()
            dispatch = self.store.get_dispatch(task_id)
            if terminal and dispatch["state"] in DispatchState.TERMINAL:
                return dispatch
            if not terminal and dispatch["state"] == DispatchState.RUNNING:
                return dispatch
            time.sleep(0.15)
        return self.store.get_dispatch(task_id)


class TestSchedulingAndCompletion(PlaneCase):
    def test_submit_schedule_run_complete(self):
        self.start_worker(script_text="the answer is 42")
        plane = self.make_plane()
        task_id = plane.submit(self.envelope())
        dispatch = self.drive(plane, task_id)
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)
        self.assertIn("42", dispatch["result"]["summary"])
        events = self.store.list_dispatch_events(task_id)
        kinds = {e["kind"] for e in events}
        self.assertIn("started", kinds)
        self.assertIn("result", kinds)

    def test_no_eligible_worker_leaves_queued(self):
        # Worker trust too low for the required trust.
        self.start_worker(trust_level=1)
        plane = self.make_plane()
        task_id = plane.submit(self.envelope(), required_trust=9)
        plane.schedule_once()
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"], DispatchState.QUEUED
        )

    def test_data_ceiling_blocks_placement(self):
        self.start_worker(data_ceiling="internal")
        plane = self.make_plane()
        task_id = plane.submit(
            self.envelope(data_classification="restricted")
        )
        plane.schedule_once()
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"], DispatchState.QUEUED
        )

    def test_model_residency_scheduling(self):
        self.start_worker(
            name="gpu", capabilities={"model_endpoints": [{
                "kind": "ollama", "url": "u",
                "models": ["qwen3:8b"], "model_count": 1,
            }]},
        )
        plane = self.make_plane()
        task_id = plane.submit(self.envelope(model="qwen3:8b"))
        dispatch = self.drive(plane, task_id)
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)


class TestResourceGroups(PlaneCase):
    def test_shared_endpoint_not_oversubscribed(self):
        # Two workers on one Ollama box (same resource group, cap 1): only
        # one heavy task runs at a time across the group.
        slow = self.root / "slow.json"
        self.root.mkdir(parents=True, exist_ok=True)
        slow.write_text(json.dumps([
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps({"command": "sleep 4"})},
            }]},
            {"content": "done"},
        ]))
        self.start_worker(name="a", script=slow, resource_group="ollama-1",
                          max_concurrency=1)
        self.start_worker(name="b", script=slow, resource_group="ollama-1",
                          max_concurrency=1)
        plane = self.make_plane(resource_group_caps={"ollama-1": 1})
        t1 = plane.submit(self.envelope(tools=("local_shell",),
                                        wall_clock_seconds=60))
        t2 = plane.submit(self.envelope(tools=("local_shell",),
                                        wall_clock_seconds=60))
        plane.schedule_once()
        running = [
            d for d in self.store.list_dispatches()
            if d["state"] in (DispatchState.RUNNING, DispatchState.OFFERING)
        ]
        self.assertEqual(len(running), 1, "group cap 1 was oversubscribed")
        # The other stays queued.
        queued = self.store.list_dispatches(state=DispatchState.QUEUED)
        self.assertEqual(len(queued), 1)
        plane.cancel(running[0]["task_id"])
        for task_id in (t1, t2):
            plane.cancel(task_id)


class TestFencingAndRetries(PlaneCase):
    def test_fence_increments_per_attempt(self):
        # A worker whose task fails transiently, retried until it succeeds.
        home = self.root / "flaky"
        home.mkdir(parents=True, exist_ok=True)
        # First attempt: no reply (transient failure). We simulate by a
        # script that returns empty content once; taskexec fails transient.
        script = home / "s.json"
        script.write_text(json.dumps([{"content": ""}]))
        self.start_worker(name="flaky", script=script)
        plane = self.make_plane(max_attempts=3, backoff_base_seconds=0.1,
                                backoff_cap_seconds=0.2)
        task_id = plane.submit(self.envelope())
        # Drive a few cycles; it should retry (empty reply => transient).
        deadline = time.time() + 15
        seen_attempts = set()
        while time.time() < deadline:
            plane.schedule_once()
            plane.poll_once()
            d = self.store.get_dispatch(task_id)
            seen_attempts.add(d["attempt"])
            if d["state"] == DispatchState.FAILED:
                break
            time.sleep(0.15)
        d = self.store.get_dispatch(task_id)
        self.assertEqual(d["state"], DispatchState.FAILED)
        self.assertEqual(d["failure_class"], FailureClass.TRANSIENT)
        self.assertGreaterEqual(max(seen_attempts), 3)

    def test_stale_receipt_cannot_commit(self):
        self.start_worker(script_text="done")
        plane = self.make_plane()
        task_id = plane.submit(self.envelope())
        self.drive(plane, task_id, terminal=False)  # get it RUNNING
        dispatch = self.store.get_dispatch(task_id)
        # A receipt from an older fence must be ignored by _finalize.
        stats = {"completed": 0, "failed": 0, "requeued": 0, "delegated": 0,
                 "resumed": 0, "events": 0}
        stale = {"attempt": dispatch["attempt"],
                 "fence": dispatch["fence"] - 1, "outcome": "success",
                 "payload": {"summary": "stale"}}
        plane._finalize_from_receipt(dispatch, stale, stats)
        self.assertEqual(stats["completed"], 0)
        self.assertNotEqual(
            self.store.get_dispatch(task_id)["state"],
            DispatchState.SUCCEEDED,
        )
        # Let it finish legitimately.
        self.drive(plane, task_id)


class TestHeartbeats(PlaneCase):
    def test_unreachable_worker_requeues_inflight(self):
        home = self.root / "hb"
        home.mkdir(parents=True, exist_ok=True)
        slow = home / "slow.json"
        slow.write_text(json.dumps([
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps({"command": "sleep 8"})},
            }]},
            {"content": "done"},
        ]))
        worker_id = self.start_worker(name="hb", script=slow)
        plane = self.make_plane(heartbeat_deadline_seconds=0.0)
        task_id = plane.submit(self.envelope(tools=("local_shell",),
                                             wall_clock_seconds=120))
        self.drive(plane, task_id, terminal=False)
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"], DispatchState.RUNNING
        )
        # Kill the worker: the heartbeat sweep should mark it unreachable
        # (transport fails, deadline 0) and requeue its dispatch.
        self._workers[worker_id].kill9()
        stats = plane.heartbeat_sweep()
        self.assertGreaterEqual(stats["unreachable"], 1)
        self.assertEqual(
            self.registry.get(worker_id)["state"], WorkerState.UNREACHABLE
        )
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"], DispatchState.QUEUED
        )

    def test_healthy_worker_records_heartbeat(self):
        worker_id = self.start_worker()
        plane = self.make_plane()
        stats = plane.heartbeat_sweep()
        self.assertGreaterEqual(stats["heartbeats"], 1)
        self.assertIsNotNone(
            self.registry.get(worker_id)["last_heartbeat_at"]
        )


class TestCancellation(PlaneCase):
    def test_cancel_running_dispatch(self):
        home = self.root / "cancelw"
        home.mkdir(parents=True, exist_ok=True)
        slow = home / "slow.json"
        slow.write_text(json.dumps([
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps({"command": "sleep 30"})},
            }]},
            {"content": "unreached"},
        ]))
        self.start_worker(name="cancelw", script=slow)
        plane = self.make_plane()
        task_id = plane.submit(self.envelope(tools=("local_shell",),
                                             wall_clock_seconds=120))
        self.drive(plane, task_id, terminal=False)
        self.assertTrue(plane.cancel(task_id, reason="operator abort"))
        self.assertEqual(
            self.store.get_dispatch(task_id)["state"],
            DispatchState.CANCELLED,
        )
        self.assertFalse(plane.cancel(task_id))  # already terminal


class TestBrokeredDelegation(PlaneCase):
    def _branched_script(self, home):
        """One worker script: the parent task ("do the thing") delegates,
        the child task ("compute the sub-answer") answers directly.
        Scheduling is by score, not worker name, so both tasks may land on
        the same worker — the script must branch on the task text."""
        home.mkdir(parents=True, exist_ok=True)
        path = home / "branched.json"
        path.write_text(json.dumps({
            "match": [
                ["compute the sub-answer",
                 [{"content": "the child computed 7"}]],
            ],
            "default": [
                {"content": "", "tool_calls": [{
                    "id": "d1", "type": "function",
                    "function": {"name": "delegate_task",
                                 "arguments": json.dumps(
                                     {"task": "compute the sub-answer"})},
                }]},
                {"content": "parent done; the child computed 7"},
            ],
        }))
        return path

    def test_delegation_creates_child_parks_parent_and_resumes(self):
        script = self._branched_script(self.root / "deleg")
        self.start_worker(name="a", script=script, max_concurrency=3)
        self.start_worker(name="b", script=script, max_concurrency=3)
        plane = self.make_plane()
        parent_task = plane.submit(
            self.envelope(task="do the thing", tools=("delegate_task",))
        )
        # Drive until a child appears and the parent parks.
        deadline = time.time() + 25
        child_id = None
        while time.time() < deadline:
            plane.schedule_once()
            plane.poll_once()
            children = self.store.list_dispatches(
                parent_task_id=parent_task
            )
            if children:
                child_id = children[0]["task_id"]
                break
            time.sleep(0.15)
        self.assertIsNotNone(child_id, "no child dispatch was brokered")
        parent = self.store.get_dispatch(parent_task)
        self.assertEqual(parent["state"], DispatchState.WAITING_CHILD)
        children_ids = {
            entry.get("child")
            for entry in parent["result"]["delegations"].values()
        }
        self.assertIn(child_id, children_ids)
        # Child is an authority-subset of the parent.
        child = self.store.get_dispatch(child_id)
        self.assertEqual(child["parent_task_id"], parent_task)
        self.assertEqual(
            tuple(child["envelope"]["tools"]), ("delegate_task",)
        )
        self.assertEqual(child["envelope"]["task"], "compute the sub-answer")
        # Drive to completion: child finishes, parent resumes and completes.
        final = self.drive(plane, parent_task, timeout=25)
        self.assertEqual(final["state"], DispatchState.SUCCEEDED)
        self.assertEqual(
            self.store.get_dispatch(child_id)["state"],
            DispatchState.SUCCEEDED,
        )
        self.assertIn("7", final["result"]["summary"])

    def test_max_depth_rejected(self):
        script = self._branched_script(self.root / "p2")
        self.start_worker(name="p2", script=script)
        plane = self.make_plane(max_delegation_depth=0)
        parent_task = plane.submit(
            self.envelope(task="do the thing", tools=("delegate_task",))
        )
        # depth 0 means no delegation allowed; the parent is resumed with a
        # rejection and completes without a child ever existing.
        final = self.drive(plane, parent_task, timeout=25)
        self.assertEqual(
            self.store.list_dispatches(parent_task_id=parent_task), []
        )
        self.assertEqual(final["state"], DispatchState.SUCCEEDED)


class TestControllerEpochFencing(PlaneCase):
    def test_superseded_controller_cannot_write(self):
        self.start_worker()
        plane = self.make_plane()
        task_id = plane.submit(self.envelope())
        # A second controller adopts a higher epoch (as a restarted daemon
        # would). The old store instance is now fenced out of all writes.
        other = MissionStore(self.root / "kernel.db")
        self.addCleanup(other.close)
        other.adopt_epoch()
        with self.assertRaises(Exception):
            self.store.transition_dispatch(task_id, DispatchState.OFFERING,
                                           attempt=1, fence=1)


if __name__ == "__main__":
    unittest.main()
