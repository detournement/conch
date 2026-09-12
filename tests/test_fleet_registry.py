"""FleetRegistry + kernel fleet tables (Swarm Phase 2 gates).

Worker/dispatch truth is event-sourced and replayable exactly like every
other kernel projection; heartbeats and worker-observed task events are
operational coordination. Admin-assigned trust labels and observed
capabilities live in separate columns with separate update paths.
"""

import tempfile
import unittest
from pathlib import Path

from conch.fleet.registry import FleetRegistry
from conch.kernel.model import (
    DispatchState,
    KernelError,
    WorkerState,
    check_dispatch_transition,
    check_worker_transition,
)
from conch.kernel.store import MissionStore
from conch.swarm.protocol import TaskEnvelope, new_id


def make_envelope(mission_id, **overrides):
    fields = {
        "task_id": new_id("task"),
        "mission_id": mission_id,
        "principal": "user",
        "task": "summarize the repository",
        "idempotency_key": new_id("task"),
        "issued_at": 1000.0,
        "tools": ("local_shell",),
        "data_classification": "internal",
    }
    fields.update(overrides)
    return TaskEnvelope(**fields)


class FleetKernelCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MissionStore(Path(self._tmp.name) / "kernel.db")
        self.addCleanup(self.store.close)
        self.registry = FleetRegistry(self.store)
        self.mission_id = self.store.create_mission({
            "goal": "fleet test mission", "budgets": {"tokens": 100000},
        })

    def enroll(self, name="box1", **kwargs):
        defaults = {
            "host": "192.0.2.10", "ssh_user": "conch",
            "trust_level": 2, "data_ceiling": "confidential",
            "capabilities": {"os": "linux"},
            "profiles": ["systemd", "process"],
            "resource_group": "ollama-box-1",
            "max_concurrency": 2,
        }
        defaults.update(kwargs)
        return self.registry.enroll(name, **defaults)


class TestWorkerRegistry(FleetKernelCase):
    def test_enroll_get_find_list(self):
        worker_id = self.enroll()
        worker = self.registry.require(worker_id)
        self.assertEqual(worker["name"], "box1")
        self.assertEqual(worker["state"], WorkerState.PENDING)
        self.assertEqual(worker["trust_level"], 2)
        self.assertEqual(worker["data_ceiling"], "confidential")
        self.assertEqual(worker["capabilities"], {"os": "linux"})
        self.assertEqual(worker["profiles"], ["systemd", "process"])
        self.assertEqual(worker["max_concurrency"], 2)
        self.assertFalse(worker["autonomy_capable"])
        self.assertEqual(
            self.registry.find("box1")["worker_id"], worker_id
        )
        self.assertEqual(len(self.registry.list()), 1)

    def test_worker_names_are_unique(self):
        self.enroll()
        with self.assertRaises(KernelError):
            self.enroll()

    def test_admin_authority_and_observed_capabilities_are_separate(self):
        worker_id = self.enroll()
        # A probe update cannot touch trust/data/labels.
        self.registry.record_probe(worker_id, {
            "os": "linux", "arch": "x86_64",
            "gpu": {"present": True, "gpus": ["RTX 4090, 24576 MiB"]},
            "profiles": ["systemd", "process", "docker"],
            "model_endpoints": [],
        })
        worker = self.registry.require(worker_id)
        self.assertEqual(worker["trust_level"], 2)
        self.assertEqual(worker["data_ceiling"], "confidential")
        self.assertTrue(worker["capabilities"]["gpu"]["present"])
        self.assertEqual(
            worker["profiles"], ["systemd", "process", "docker"]
        )
        # Authority changes are explicit admin calls.
        self.registry.assign_authority(
            worker_id, trust_level=5, data_ceiling="restricted",
            labels={"zone": "home-lab"},
        )
        worker = self.registry.require(worker_id)
        self.assertEqual(worker["trust_level"], 5)
        self.assertEqual(worker["data_ceiling"], "restricted")
        self.assertEqual(worker["labels"], {"zone": "home-lab"})

    def test_unknown_update_fields_fail_closed(self):
        worker_id = self.enroll()
        with self.assertRaises(KernelError):
            self.store.update_worker(worker_id, {"state": "active"})
        with self.assertRaises(KernelError):
            self.store.update_worker(worker_id, {"evil_field": 1})

    def test_state_machine_enforced(self):
        worker_id = self.enroll()
        self.registry.activate(worker_id)
        self.assertEqual(
            self.registry.require(worker_id)["state"], WorkerState.ACTIVE
        )
        self.registry.drain(worker_id, "maintenance")
        self.registry.transition(worker_id, WorkerState.OFFLINE)
        self.registry.activate(worker_id)
        self.registry.quarantine(worker_id, "suspicious output")
        self.registry.revoke(worker_id, "decommissioned")
        worker = self.registry.require(worker_id)
        self.assertEqual(worker["state"], WorkerState.REVOKED)
        # REVOKED is terminal.
        with self.assertRaises(KernelError):
            self.registry.activate(worker_id)

    def test_illegal_transitions_fail_closed(self):
        with self.assertRaises(KernelError):
            check_worker_transition("pending", "draining")
        with self.assertRaises(KernelError):
            check_worker_transition("revoked", "active")
        with self.assertRaises(KernelError):
            check_worker_transition("nonsense", "active")
        worker_id = self.enroll()
        with self.assertRaises(KernelError):
            self.registry.transition(worker_id, "nonsense")

    def test_version_conflicts_detected(self):
        worker_id = self.enroll()
        from conch.kernel.model import ConflictError

        with self.assertRaises(ConflictError):
            self.store.transition_worker(
                worker_id, WorkerState.ACTIVE, expected_version=99
            )

    def test_heartbeats_are_operational_not_events(self):
        worker_id = self.enroll()
        before = self.store.event_count()
        self.assertTrue(self.registry.heartbeat(worker_id, 5))
        self.assertFalse(self.registry.heartbeat(worker_id, 3))
        self.assertEqual(self.store.event_count(), before)
        worker = self.registry.require(worker_id)
        self.assertEqual(worker["heartbeat_seq"], 5)
        self.assertIsNotNone(worker["last_heartbeat_at"])
        # And the store still replays exactly (heartbeats not replayed).
        report = self.store.verify_integrity()
        self.assertEqual(report["replay"], "match")

    def test_incarnation_bump(self):
        worker_id = self.enroll()
        self.registry.bump_incarnation(worker_id)
        self.registry.bump_incarnation(worker_id)
        self.assertEqual(self.registry.require(worker_id)["incarnation"], 2)


class TestEligibility(FleetKernelCase):
    def _active_worker(self, **kwargs):
        worker_id = self.enroll(**kwargs)
        self.registry.activate(worker_id)
        return self.registry.require(worker_id)

    def test_state_and_protocol_and_trust_filters(self):
        envelope = make_envelope(self.mission_id)
        worker = self._active_worker()
        self.assertTrue(self.registry.eligible(worker, envelope))
        self.assertFalse(
            self.registry.eligible(worker, envelope, required_trust=99)
        )
        pending = self.registry.require(self.enroll(name="pending-box"))
        self.assertFalse(self.registry.eligible(pending, envelope))
        stale = dict(worker, protocol_min=99, protocol_max=99)
        self.assertFalse(self.registry.eligible(stale, envelope))

    def test_data_classification_ceiling(self):
        worker = self._active_worker(data_ceiling="internal")
        public = make_envelope(self.mission_id, data_classification="public")
        secretive = make_envelope(
            self.mission_id, data_classification="restricted"
        )
        self.assertTrue(self.registry.eligible(worker, public))
        self.assertFalse(self.registry.eligible(worker, secretive))

    def test_model_residency(self):
        worker = self._active_worker(capabilities={
            "model_endpoints": [{
                "kind": "ollama", "url": "http://127.0.0.1:11434",
                "models": ["qwen3:8b", "llama3.2:3b"], "model_count": 2,
            }],
        })
        with_model = make_envelope(self.mission_id, model="qwen3:8b")
        bare_name = make_envelope(self.mission_id, model="llama3.2")
        missing = make_envelope(self.mission_id, model="gpt-oss:120b")
        self.assertTrue(self.registry.eligible(worker, with_model))
        self.assertTrue(self.registry.eligible(worker, bare_name))
        self.assertFalse(self.registry.eligible(worker, missing))


class TestDispatches(FleetKernelCase):
    def test_create_and_transition_lifecycle(self):
        envelope = make_envelope(self.mission_id)
        task_id = self.store.create_dispatch(
            envelope.to_dict(), max_attempts=2
        )
        self.assertEqual(task_id, envelope.task_id)
        dispatch = self.store.get_dispatch(task_id)
        self.assertEqual(dispatch["state"], DispatchState.QUEUED)
        self.assertEqual(dispatch["max_attempts"], 2)
        self.assertEqual(dispatch["envelope"]["task"], envelope.task)
        self.store.transition_dispatch(
            task_id, DispatchState.OFFERING, attempt=1,
            worker_id="wrk-0000000000001-0000000000000001", fence=7,
        )
        self.store.transition_dispatch(task_id, DispatchState.RUNNING)
        self.store.transition_dispatch(
            task_id, DispatchState.SUCCEEDED,
            result={"summary": "done"},
        )
        dispatch = self.store.get_dispatch(task_id)
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)
        self.assertEqual(dispatch["attempt"], 1)
        self.assertEqual(dispatch["fence"], 7)
        self.assertEqual(dispatch["result"], {"summary": "done"})
        # Terminal is terminal.
        with self.assertRaises(KernelError):
            self.store.transition_dispatch(task_id, DispatchState.QUEUED)

    def test_invalid_envelope_fails_closed(self):
        envelope = make_envelope(self.mission_id).to_dict()
        envelope["extra_field"] = "smuggled"
        with self.assertRaises(KernelError):
            self.store.create_dispatch(envelope)

    def test_dispatch_requires_existing_mission(self):
        envelope = make_envelope(
            "msn-0000000000001-0000000000000001"
        )
        with self.assertRaises(KernelError):
            self.store.create_dispatch(envelope.to_dict())

    def test_duplicate_dispatch_rejected(self):
        envelope = make_envelope(self.mission_id)
        self.store.create_dispatch(envelope.to_dict())
        with self.assertRaises(KernelError):
            self.store.create_dispatch(envelope.to_dict())

    def test_illegal_dispatch_transitions_fail_closed(self):
        with self.assertRaises(KernelError):
            check_dispatch_transition("queued", "running")
        with self.assertRaises(KernelError):
            check_dispatch_transition("succeeded", "queued")
        with self.assertRaises(KernelError):
            check_dispatch_transition("queued", "bogus")

    def test_count_worker_dispatches(self):
        worker_id = "wrk-0000000000001-00000000000000aa"
        for index in range(3):
            envelope = make_envelope(self.mission_id)
            self.store.create_dispatch(envelope.to_dict())
            self.store.transition_dispatch(
                envelope.task_id, DispatchState.OFFERING, attempt=1,
                worker_id=worker_id, fence=index + 1,
            )
        self.assertEqual(self.store.count_worker_dispatches(worker_id), 3)

    def test_dispatch_events_dedupe_on_redelivery(self):
        envelope = make_envelope(self.mission_id)
        self.store.create_dispatch(envelope.to_dict())
        batch = [
            {"task_id": envelope.task_id, "attempt": 1, "sequence": 0,
             "kind": "started", "payload": {}},
            {"task_id": envelope.task_id, "attempt": 1, "sequence": 1,
             "kind": "log", "payload": {"line": "working"}},
        ]
        self.assertEqual(self.store.record_dispatch_events(batch), 2)
        # Redelivery of the same batch inserts nothing.
        self.assertEqual(self.store.record_dispatch_events(batch), 0)
        events = self.store.list_dispatch_events(envelope.task_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["payload"], {"line": "working"})
        tail = self.store.list_dispatch_events(
            envelope.task_id, since_seq=0
        )
        self.assertEqual(len(tail), 1)


class TestFleetReplay(FleetKernelCase):
    def test_mixed_fleet_workload_replays_exactly(self):
        worker_id = self.enroll()
        self.registry.activate(worker_id)
        self.registry.record_probe(worker_id, {
            "os": "linux", "profiles": ["systemd"],
        })
        self.registry.assign_authority(worker_id, trust_level=4)
        self.registry.record_deployment(
            worker_id, artifact_digest="a" * 64, runtime_profile="systemd",
        )
        self.registry.bump_incarnation(worker_id)
        self.registry.heartbeat(worker_id, 12)
        envelope = make_envelope(self.mission_id)
        self.store.create_dispatch(envelope.to_dict())
        self.store.transition_dispatch(
            envelope.task_id, DispatchState.OFFERING, attempt=1,
            worker_id=worker_id, fence=1,
        )
        self.store.transition_dispatch(
            envelope.task_id, DispatchState.RUNNING
        )
        self.store.record_dispatch_events([{
            "task_id": envelope.task_id, "attempt": 1, "sequence": 0,
            "kind": "started", "payload": {},
        }])
        self.store.transition_dispatch(
            envelope.task_id, DispatchState.FAILED,
            failure_class="transient", error="worker died",
        )
        self.registry.drain(worker_id, "post-test")
        report = self.store.verify_integrity()
        self.assertEqual(report["replay"], "match")
        self.assertGreater(report["events_verified"], 10)


if __name__ == "__main__":
    unittest.main()
