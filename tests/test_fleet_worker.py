"""Worker supervisor + task executor gates (Swarm Phase 2).

The supervisor runs as a real local subprocess; the controller side talks
to its real unix socket through the fake SSH transport, so the offer/start
receipt handshake, fencing, bounded queue, event spool, cancellation, and
brokered-delegation parking under test are the production code paths.
Task subprocesses execute the real chat_turn driven by a scripted raw_fn
(no live model needed).
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from conch.swarm.protocol import (
    ProtocolError,
    RpcRequest,
    RpcResponse,
    TaskEnvelope,
    new_id,
)

from tests.fleet_fakes import (
    FakeSSHWorkerTransport,
    LocalWorkerProcess,
)


def make_envelope(**overrides):
    mission_id = overrides.pop("mission_id", new_id("msn"))
    fields = {
        "task_id": new_id("task"),
        "mission_id": mission_id,
        "principal": "user",
        "task": "say hello",
        "idempotency_key": new_id("task"),
        "issued_at": 1000.0,
        "tools": (),
        "max_tool_rounds": 3,
        "wall_clock_seconds": 30,
    }
    fields.update(overrides)
    return TaskEnvelope(**fields)


def script_file(home: Path, name: str, responses) -> Path:
    path = home / f"{name}.json"
    path.write_text(json.dumps(responses), encoding="utf-8")
    return path


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "worker"

    def start_worker(self, env_extra=None):
        worker = LocalWorkerProcess(self.home, env_extra=env_extra)
        worker.start()
        self.addCleanup(worker.stop)
        return worker

    def offer(self, transport, envelope, attempt=1, fence=1, epoch=1):
        return transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.offer",
            args={"envelope": envelope.to_dict(), "attempt": attempt,
                  "fence": fence, "controller_epoch": epoch},
        ))

    def start(self, transport, envelope, attempt=1, fence=1):
        return transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.start",
            args={"task_id": envelope.task_id, "attempt": attempt,
                  "fence": fence},
        ))

    def wait_terminal(self, transport, envelope, timeout=25.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            resp = transport.send(RpcRequest(
                rpc_id=new_id("rpc"), op="task.status",
                args={"task_id": envelope.task_id},
            ))
            last = resp.result
            if last["state"] in ("completed", "failed", "cancelled"):
                return last
            time.sleep(0.1)
        raise AssertionError(f"task never terminal; last={last}")


class TestRpcProtocolShapes(unittest.TestCase):
    def test_request_op_must_be_known(self):
        with self.assertRaises(ProtocolError):
            RpcRequest(rpc_id=new_id("rpc"), op="task.evict")

    def test_response_failure_requires_class(self):
        with self.assertRaises(ProtocolError):
            RpcResponse(rpc_id=new_id("rpc"), ok=False, error="boom")
        with self.assertRaises(ProtocolError):
            RpcResponse(rpc_id=new_id("rpc"), ok=False, error="boom",
                        error_class="not-a-class")

    def test_response_success_rejects_error(self):
        with self.assertRaises(ProtocolError):
            RpcResponse(rpc_id=new_id("rpc"), ok=True, error="x",
                        error_class="bug")

    def test_roundtrip_canonical(self):
        req = RpcRequest(rpc_id=new_id("rpc"), op="task.status",
                         args={"task_id": "t"})
        self.assertEqual(RpcRequest.from_json(req.to_json()), req)


class TestWorkerLifecycle(WorkerCase):
    def _hello_script(self):
        return script_file(self.home, "hello", [
            {"content": "hello from the worker"},
        ])

    def test_status_before_any_task(self):
        worker = self.start_worker()
        transport = FakeSSHWorkerTransport(worker)
        resp = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="worker.status", args={},
        ))
        self.assertTrue(resp.ok)
        self.assertEqual(resp.result["queue"]["in_flight"], 0)
        self.assertGreaterEqual(resp.result["incarnation"], 1)

    def test_offer_start_run_to_completion(self):
        self.home.mkdir(parents=True, exist_ok=True)
        script = self._hello_script()
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(script)}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelope = make_envelope()
        offer = self.offer(transport, envelope)
        self.assertTrue(offer.ok)
        self.assertFalse(offer.result["duplicate"])
        self.assertEqual(offer.result["state"], "offered")
        self.assertIn("receipt_id", offer.result)
        started = self.start(transport, envelope)
        self.assertTrue(started.ok)
        final = self.wait_terminal(transport, envelope)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["receipt"]["outcome"], "success")
        events = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.events",
            args={"task_id": envelope.task_id},
        )).result
        kinds = [e["kind"] for e in events["events"]]
        self.assertIn("started", kinds)
        self.assertIn("result", kinds)

    def test_duplicate_offer_returns_prior_receipt(self):
        self.home.mkdir(parents=True, exist_ok=True)
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(self._hello_script())}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelope = make_envelope()
        first = self.offer(transport, envelope)
        second = self.offer(transport, envelope)
        third = self.offer(transport, envelope)
        self.assertFalse(first.result["duplicate"])
        self.assertTrue(second.result["duplicate"])
        self.assertTrue(third.result["duplicate"])
        self.assertEqual(
            first.result["receipt_id"], second.result["receipt_id"]
        )
        self.assertEqual(
            first.result["receipt_id"], third.result["receipt_id"]
        )

    def test_duplicate_start_is_idempotent(self):
        self.home.mkdir(parents=True, exist_ok=True)
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(self._hello_script())}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelope = make_envelope()
        self.offer(transport, envelope)
        a = self.start(transport, envelope)
        b = self.start(transport, envelope)
        self.assertTrue(a.ok and b.ok)
        self.assertTrue(b.result["duplicate"])
        self.wait_terminal(transport, envelope)


class TestFencing(WorkerCase):
    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(
            json.dumps({"agent_mode": True}), encoding="utf-8"
        )
        # A slow task so a superseding offer arrives mid-run.
        self.script = script_file(self.home, "slow", [
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps(
                                 {"command": "sleep 2"})},
            }]},
            {"content": "done"},
        ])
        self.worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(self.script)}
        )
        self.transport = FakeSSHWorkerTransport(self.worker)

    def test_stale_fence_offer_rejected(self):
        envelope = make_envelope(tools=("local_shell",))
        self.offer(self.transport, envelope, attempt=1, fence=5, epoch=2)
        stale = self.offer(
            self.transport, envelope, attempt=1, fence=4, epoch=2
        )
        self.assertFalse(stale.ok)
        self.assertEqual(stale.error_class, "policy")
        self.assertIn("stale fence", stale.error)

    def test_stale_epoch_offer_rejected(self):
        envelope = make_envelope(tools=("local_shell",))
        self.offer(self.transport, envelope, attempt=1, fence=1, epoch=5)
        stale = self.offer(
            self.transport, envelope, attempt=2, fence=1, epoch=4
        )
        self.assertFalse(stale.ok)
        self.assertEqual(stale.error_class, "policy")

    def test_start_with_wrong_fence_rejected(self):
        envelope = make_envelope(tools=("local_shell",))
        self.offer(self.transport, envelope, attempt=1, fence=7, epoch=1)
        resp = self.transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.start",
            args={"task_id": envelope.task_id, "attempt": 1, "fence": 6},
        ))
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error_class, "policy")

    def test_newer_fence_supersedes_old_attempt(self):
        envelope = make_envelope(tools=("local_shell",))
        self.offer(self.transport, envelope, attempt=1, fence=1, epoch=1)
        self.start(self.transport, envelope, attempt=1, fence=1)
        time.sleep(0.3)
        # A new controller epoch re-offers the same task with a higher
        # fence: the old attempt is superseded (its process killed).
        superseded = self.offer(
            self.transport, envelope, attempt=2, fence=2, epoch=2
        )
        self.assertTrue(superseded.ok)
        self.assertFalse(superseded.result["duplicate"])
        status = self.transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.status",
            args={"task_id": envelope.task_id},
        )).result
        self.assertEqual(status["fence"], 2)
        self.assertEqual(status["attempt"], 2)


class TestBoundedQueue(WorkerCase):
    def test_queue_full_rejects_with_retry_after(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(
            json.dumps({"max_queue": 2, "max_concurrency": 2,
                        "agent_mode": True}),
            encoding="utf-8",
        )
        script = script_file(self.home, "slow", [
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps(
                                 {"command": "sleep 5"})},
            }]},
            {"content": "done"},
        ])
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(script)}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelopes = [make_envelope(tools=("local_shell",)) for _ in range(3)]
        for index, envelope in enumerate(envelopes[:2]):
            resp = self.offer(transport, envelope, fence=index + 1)
            self.assertTrue(resp.ok)
            self.start(transport, envelope, fence=index + 1)
        third = self.offer(transport, envelopes[2], fence=3)
        self.assertFalse(third.ok)
        self.assertEqual(third.error_class, "resource")
        self.assertGreater(third.retry_after, 0)


class TestCancellation(WorkerCase):
    def test_cancel_kills_process_group_and_records_receipt(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(
            json.dumps({"agent_mode": True}), encoding="utf-8"
        )
        script = script_file(self.home, "slow", [
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps(
                                 {"command": "sleep 30"})},
            }]},
            {"content": "unreached"},
        ])
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(script)}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelope = make_envelope(tools=("local_shell",),
                                 wall_clock_seconds=120)
        self.offer(transport, envelope)
        started = self.start(transport, envelope)
        self.assertTrue(started.ok)
        time.sleep(0.5)
        status = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.status",
            args={"task_id": envelope.task_id},
        )).result
        self.assertEqual(status["state"], "running")
        pid = self._task_pid(envelope.task_id)
        cancel = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.cancel",
            args={"task_id": envelope.task_id, "reason": "operator"},
        ))
        self.assertTrue(cancel.ok)
        self.assertEqual(cancel.result["state"], "cancelled")
        # Idempotent cancel.
        again = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.cancel",
            args={"task_id": envelope.task_id},
        ))
        self.assertTrue(again.result["duplicate"])
        if pid:
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.1)
            self.assertFalse(_pid_alive(pid), "task process survived cancel")

    def _task_pid(self, task_id):
        import sqlite3

        db = sqlite3.connect(str(self.home / "state" / "worker.db"))
        try:
            row = db.execute(
                "SELECT pid FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return int(row[0]) if row and row[0] else 0
        finally:
            db.close()


class TestEventSpoolAndAck(WorkerCase):
    def test_events_survive_restart_and_ack_watermark(self):
        self.home.mkdir(parents=True, exist_ok=True)
        script = script_file(self.home, "hello", [{"content": "hi"}])
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(script)}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelope = make_envelope()
        self.offer(transport, envelope)
        self.start(transport, envelope)
        self.wait_terminal(transport, envelope)
        first = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.events",
            args={"task_id": envelope.task_id},
        )).result
        self.assertGreaterEqual(len(first["events"]), 2)
        ack = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.events_ack",
            args={"task_id": envelope.task_id,
                  "upto_seq": first["watermark"]},
        ))
        self.assertTrue(ack.ok)
        # Restart the supervisor: the spool and ack watermark persist.
        worker.stop()
        worker2 = LocalWorkerProcess(
            self.home,
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(script)},
        ).start()
        self.addCleanup(worker2.stop)
        transport2 = FakeSSHWorkerTransport(worker2)
        status = transport2.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.status",
            args={"task_id": envelope.task_id},
        )).result
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["acked_seq"], first["watermark"])
        tail = transport2.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.events",
            args={"task_id": envelope.task_id,
                  "since_seq": first["watermark"]},
        )).result
        self.assertEqual(tail["events"], [])


class TestBrokeredDelegationParking(WorkerCase):
    def test_delegate_parks_waiting_child_then_resumes(self):
        self.home.mkdir(parents=True, exist_ok=True)
        # First segment: model calls delegate_task. After resume: it sees
        # the child's tool result and finishes.
        script = script_file(self.home, "deleg", [
            {"content": "", "tool_calls": [{
                "id": "d1", "type": "function",
                "function": {"name": "delegate_task",
                             "arguments": json.dumps(
                                 {"task": "research the thing"})},
            }]},
            {"content": "done using the child's findings"},
        ])
        worker = self.start_worker(
            env_extra={"CONCH_FLEET_TASK_SCRIPT": str(script)}
        )
        transport = FakeSSHWorkerTransport(worker)
        envelope = make_envelope(tools=("delegate_task",))
        self.offer(transport, envelope)
        self.start(transport, envelope)
        # It parks waiting_child.
        deadline = time.time() + 15
        state = ""
        while time.time() < deadline:
            state = transport.send(RpcRequest(
                rpc_id=new_id("rpc"), op="task.status",
                args={"task_id": envelope.task_id},
            )).result["state"]
            if state == "waiting_child":
                break
            time.sleep(0.1)
        self.assertEqual(state, "waiting_child")
        events = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.events",
            args={"task_id": envelope.task_id},
        )).result["events"]
        deleg = [e for e in events if e["kind"] == "delegation_requested"]
        self.assertEqual(len(deleg), 1)
        self.assertEqual(deleg[0]["payload"]["task"], "research the thing")
        # Controller resumes with the child result folded in as a tool
        # result group.
        resume = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="task.resume",
            args={"task_id": envelope.task_id, "attempt": 1, "fence": 1,
                  "tool_call_id": deleg[0]["payload"]["delegation_id"],
                  "result_text": "the child found the answer: 42"},
        ))
        self.assertTrue(resume.ok)
        final = self.wait_terminal(transport, envelope)
        self.assertEqual(final["state"], "completed")
        self.assertIn(
            "child", final["receipt"]["payload"]["summary"].lower()
        )


class TestArtifactTransfer(WorkerCase):
    def test_content_addressed_put_get_with_digest_verification(self):
        import base64
        import hashlib

        self.home.mkdir(parents=True, exist_ok=True)
        worker = self.start_worker()
        transport = FakeSSHWorkerTransport(worker)
        payload = os.urandom(1000)
        digest = hashlib.sha256(payload).hexdigest()
        put = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="artifact.put",
            args={"digest": digest, "offset": 0, "total_size": len(payload),
                  "data_b64": base64.b64encode(payload).decode()},
        ))
        self.assertTrue(put.ok)
        self.assertTrue(put.result["complete"])
        get = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="artifact.get",
            args={"digest": digest, "offset": 0, "length": 1000},
        ))
        self.assertTrue(get.ok)
        self.assertEqual(base64.b64decode(get.result["data_b64"]), payload)
        self.assertTrue(get.result["eof"])

    def test_put_with_wrong_digest_stores_nothing(self):
        import base64

        self.home.mkdir(parents=True, exist_ok=True)
        worker = self.start_worker()
        transport = FakeSSHWorkerTransport(worker)
        payload = b"lying about the digest"
        put = transport.send(RpcRequest(
            rpc_id=new_id("rpc"), op="artifact.put",
            args={"digest": "a" * 64, "offset": 0,
                  "total_size": len(payload),
                  "data_b64": base64.b64encode(payload).decode()},
        ))
        self.assertFalse(put.ok)
        self.assertIn("digest mismatch", put.error)


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


if __name__ == "__main__":
    unittest.main()
