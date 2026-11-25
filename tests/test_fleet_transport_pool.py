"""Cheap controller ticks: shared SSH masters, per-worker backoff, the
sweep-reused skill inventory, and streamed artifacts.

No SSH host is reachable here, so ``ssh`` is replaced at its two seams: the
master spawner (a fake that binds a real unix socket at the ControlPath,
like ``ssh -M -f`` would) and the per-RPC runner (an in-memory worker).
Streamed artifacts run the real ``conch-hostctl`` locally in place of the
remote end of the ssh session.
"""

import base64
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.fleet.controller import (
    ArtifactPuller,
    ControllerDaemon,
    PooledTransportFactory,
    ensure_adhoc_mission,
)
from conch.fleet.plane import PlaneError, TaskPlane
from conch.fleet.registry import FleetRegistry
from conch.fleet.transport import (
    ArtifactTransferError,
    SSHConnectionPool,
    TransportError,
    WorkerTransport,
)
from conch.kernel.model import DispatchState
from conch.kernel.store import MissionStore
from conch.ssh_control import SSHControlManager, SSHTarget
from conch.swarm.protocol import RpcRequest, RpcResponse, TaskEnvelope, new_id

ROOT = Path(__file__).resolve().parents[1]


class FakeMasters:
    """Spawner standing in for ``ssh -M -f``: binds the ControlPath."""

    def __init__(self):
        self.spawns = []
        self.dead_hosts = set()
        self.stderr = b"ssh: connect to host port 22: Connection timed out"
        self._sockets = []

    def __call__(self, argv, timeout):
        self.spawns.append(list(argv))
        if argv[-1] in self.dead_hosts:
            return 255, self.stderr
        path = argv[argv.index("-S") + 1]
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(64)
        self._sockets.append(server)

        def accept_loop(sock=server):
            while True:
                try:
                    conn, _ = sock.accept()
                    conn.close()
                except OSError:
                    return

        threading.Thread(target=accept_loop, daemon=True).start()
        return 0, b""

    def kill_all(self):
        """The masters die without removing their sockets (kill -9)."""
        for server in self._sockets:
            server.close()
        self._sockets = []

    def close(self):
        self.kill_all()


class FakeWorkers:
    """In-memory worker side of ``conch-hostctl rpc`` (one per name)."""

    def __init__(self):
        self.calls = []
        self.down = set()
        self.tasks = {}
        self.artifacts = {}
        self.skills = ["summarize"]

    def runner(self, argv, stdin_bytes, timeout):
        worker = argv[-1].split("--worker", 1)[1].split()[0]
        request = RpcRequest.from_json(stdin_bytes.strip())
        self.calls.append((worker, request.op))
        if worker in self.down:
            return 255, b"", b"Connection timed out"
        args = request.args
        result = {}
        if request.op == "worker.status":
            result = {"heartbeat_seq": 1, "skills": list(self.skills)}
        elif request.op == "task.offer":
            env = args["envelope"]
            self.tasks[env["task_id"]] = {
                "state": "offered", "attempt": args["attempt"],
                "fence": args["fence"], "events": [],
            }
            result = {"receipt_id": "r1"}
        elif request.op == "task.start":
            self.tasks[args["task_id"]]["state"] = "running"
            result = {"state": "running"}
        elif request.op == "task.events":
            task = self.tasks[args["task_id"]]
            since = int(args.get("since_seq", -1))
            batch = [
                {"task_id": args["task_id"], "attempt": task["attempt"],
                 "sequence": seq, "kind": "log", "failure_class": "",
                 "payload": {}, "created_at": 1.0}
                for seq in range(since + 1, len(task["events"]))
            ]
            result = {"events": batch,
                      "watermark": len(task["events"]) - 1,
                      "attempt": task["attempt"], "state": task["state"]}
        elif request.op == "task.events_ack":
            result = {}
        elif request.op == "task.status":
            task = self.tasks[args["task_id"]]
            result = {"state": task["state"],
                      "receipt": task.get("receipt")}
        elif request.op == "artifact.get":
            data = self.artifacts[args["digest"]]
            offset = int(args["offset"])
            piece = data[offset:offset + int(args["length"])]
            result = {"data_b64": base64.b64encode(piece).decode("ascii"),
                      "eof": offset + len(piece) >= len(data)}
        response = RpcResponse(rpc_id=request.rpc_id, ok=True, result=result)
        return 0, response.to_json().encode() + b"\n", b""

    def complete(self, task_id, artifacts=()):
        task = self.tasks[task_id]
        task["state"] = "completed"
        task["receipt"] = {
            "attempt": task["attempt"], "fence": task["fence"],
            "outcome": "success",
            "payload": {"summary": "ok", "artifacts": list(artifacts)},
        }

    def ops(self, op):
        return [c for c in self.calls if c[1] == op]


def short_tmpdir(case):
    # ControlPaths are unix sockets (~104-byte limit): keep them short.
    path = tempfile.mkdtemp(prefix="cp")
    case.addCleanup(lambda: __import__("shutil").rmtree(path, True))
    return Path(path)


class TestSSHConnectionPool(unittest.TestCase):
    def setUp(self):
        self.runtime = short_tmpdir(self) / "s"
        self.manager = SSHControlManager(runtime_dir=self.runtime)
        self.masters = FakeMasters()
        self.addCleanup(self.masters.close)
        self.pool = SSHConnectionPool(self.manager, spawner=self.masters)
        self.target = SSHTarget(host="fleet-a", user="conch")

    def test_master_started_once_and_reused(self):
        for _ in range(20):
            self.pool.ensure(self.target)
        self.assertEqual(len(self.masters.spawns), 1)
        self.assertEqual(self.pool.masters_started, 1)
        argv = self.masters.spawns[0]
        self.assertIn("-M", argv)
        self.assertIn("-f", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=10", argv)
        self.assertIn(
            f"ControlPersist={self.manager.persist_seconds}", argv
        )
        # Every exec rides the same ControlPath with ControlMaster=no.
        exec_argv = self.manager.exec_argv(self.target, "true")
        self.assertEqual(
            exec_argv[exec_argv.index("-S") + 1],
            argv[argv.index("-S") + 1],
        )
        self.assertIn("ControlMaster=no", exec_argv)

    def test_default_pool_uses_fleet_idle_timeout(self):
        pool = SSHConnectionPool()
        self.addCleanup(pool.close)
        self.assertEqual(pool.manager.persist_seconds, 300)

    def test_dead_master_is_reestablished(self):
        self.pool.ensure(self.target)
        self.masters.kill_all()  # stale socket file, nobody listening
        self.pool.ensure(self.target)
        self.assertEqual(self.pool.masters_started, 2)

    def test_unreachable_host_backs_off_without_spawning(self):
        self.masters.dead_hosts.add("fleet-a")
        with self.assertRaises(TransportError) as ctx:
            self.pool.ensure(self.target)
        self.assertIn("timed out", str(ctx.exception))
        for _ in range(5):
            with self.assertRaises(TransportError) as ctx:
                self.pool.ensure(self.target)
            self.assertIn("backing off", str(ctx.exception))
        self.assertEqual(len(self.masters.spawns), 1)

    def test_backoff_expires_and_recovers(self):
        now = [100.0]
        pool = SSHConnectionPool(
            self.manager, spawner=self.masters, clock=lambda: now[0],
        )
        self.masters.dead_hosts.add("fleet-a")
        with self.assertRaises(TransportError):
            pool.ensure(self.target)
        self.masters.dead_hosts.clear()
        now[0] += 60.0
        pool.ensure(self.target)
        self.assertEqual(pool.masters_started, 1)

    def test_mux_unavailable_falls_back_to_direct(self):
        self.masters.dead_hosts.add("fleet-a")
        self.masters.stderr = (
            b"unix_listener: path too long for Unix domain socket"
        )
        self.pool.ensure(self.target)  # no raise: direct sessions instead
        self.pool.ensure(self.target)
        self.assertEqual(len(self.masters.spawns), 1)

    def test_runtime_dir_safety_still_enforced(self):
        real = short_tmpdir(self) / "real"
        real.mkdir()
        link = short_tmpdir(self) / "link"
        link.symlink_to(real)
        pool = SSHConnectionPool(
            SSHControlManager(runtime_dir=link), spawner=self.masters,
        )
        with self.assertRaises(TransportError):
            pool.ensure(self.target)
        self.assertEqual(self.masters.spawns, [])

    def test_close_stops_owned_masters(self):
        self.pool.ensure(self.target)
        control = self.manager.control_path(self.target)
        with patch("conch.ssh_control.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            self.pool.close()
        exits = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any("-O" in a and "exit" in a for a in exits))
        self.assertFalse(control.exists())
        with self.assertRaises(TransportError):
            self.pool.ensure(self.target)

    def test_interactive_connect_argv_unchanged(self):
        argv = SSHControlManager(
            runtime_dir=short_tmpdir(self) / "i"
        ).connect_argv(self.target)
        self.assertNotIn("BatchMode=yes", argv)
        self.assertNotIn("ConnectTimeout=10", argv)
        self.assertIn("ControlMaster=yes", argv)

    def test_transport_invalidates_master_on_ssh_failure(self):
        workers = FakeWorkers()
        transport = WorkerTransport(
            self.target, "w1", pool=self.pool, runner=workers.runner,
        )
        self.assertTrue(transport.call("worker.status").ok)
        workers.down.add("w1")
        with patch("conch.ssh_control.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            with self.assertRaises(TransportError):
                transport.call("worker.status")
        workers.down.clear()
        self.assertTrue(transport.call("worker.status").ok)
        self.assertEqual(self.pool.masters_started, 2)


class PlaneHarness(unittest.TestCase):
    def setUp(self):
        self.root = short_tmpdir(self)
        self.store = MissionStore(self.root / "kernel.db")
        self.addCleanup(self.store.close)
        self.store.adopt_epoch()
        self.registry = FleetRegistry(self.store)
        self.mission_id = self.store.create_mission({
            "goal": "pool test", "budgets": {},
        })
        self.workers = FakeWorkers()
        self.masters = FakeMasters()
        self.addCleanup(self.masters.close)
        self.pool = SSHConnectionPool(
            SSHControlManager(runtime_dir=self.root / "s"),
            spawner=self.masters,
        )

    def factory(self, worker):
        # Streaming is exercised in TestStreamedArtifacts; here the host
        # looks like an older hostctl so pulls use the chunked RPCs.
        return WorkerTransport(
            SSHTarget(host=worker["host"]), worker["name"],
            pool=self.pool, runner=self.workers.runner,
            popen=old_hostctl_popen,
        )

    def enroll(self, name, host):
        worker_id = self.registry.enroll(
            name, host=host, trust_level=2, data_ceiling="confidential",
            max_concurrency=16, profiles=["process"],
            capabilities={"os": "linux"},
        )
        self.registry.activate(worker_id)
        return worker_id

    def submit(self, plane, count):
        return [plane.submit(TaskEnvelope(
            task_id=new_id("task"), mission_id=self.mission_id,
            principal="user", task=f"t{i}", idempotency_key=new_id("task"),
            issued_at=1.0,
        )) for i in range(count)]


class TestPlaneTickCost(PlaneHarness):
    def test_steady_poll_is_one_call_per_task_over_one_master(self):
        self.enroll("w1", "host-1")
        plane = TaskPlane(self.store, self.registry, self.factory)
        tasks = self.submit(plane, 5)
        plane.tick()
        self.workers.calls.clear()
        plane.tick()
        # Running, no new events: task.events only (no task.status).
        self.assertEqual(len(self.workers.calls), 5)
        self.assertEqual(len(self.workers.ops("task.events")), 5)
        self.assertEqual(self.pool.masters_started, 1)
        # Once the worker reports a terminal state, task.status fetches
        # the receipt and the dispatch finalizes.
        self.workers.complete(tasks[0])
        plane.tick()
        self.assertEqual(len(self.workers.ops("task.status")), 1)
        self.assertEqual(
            self.store.get_dispatch(tasks[0])["state"],
            DispatchState.SUCCEEDED,
        )

    def test_failed_worker_skipped_for_rest_of_tick_and_backed_off(self):
        self.enroll("w1", "host-1")
        self.enroll("w2", "host-2")
        plane = TaskPlane(self.store, self.registry, self.factory)
        self.submit(plane, 8)
        plane.tick()
        self.workers.calls.clear()
        self.workers.down.add("w1")
        with patch("conch.ssh_control.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            stats = plane.tick()
        w1_calls = [c for c in self.workers.calls if c[0] == "w1"]
        self.assertEqual(len(w1_calls), 1)
        self.assertGreaterEqual(stats.get("skipped", 0), 1)
        # The heartbeat sweep in the same tick does not retry it either.
        plane.heartbeat_sweep()
        self.assertEqual(
            len([c for c in self.workers.calls if c[0] == "w1"]), 1
        )
        # w2 is untouched by w1's failure.
        self.assertTrue(any(c[0] == "w2" for c in self.workers.calls))
        # Next tick: still inside the time backoff, so nothing spawned.
        plane.tick()
        self.assertEqual(
            len([c for c in self.workers.calls if c[0] == "w1"]), 1
        )
        # The dead host's master is not respawned per call either.
        self.assertLessEqual(len(self.masters.spawns), 3)

    def test_backoff_expires(self):
        self.enroll("w1", "host-1")
        plane = TaskPlane(self.store, self.registry, self.factory, config={
            "transport_backoff_base_seconds": 0.05,
        })
        self.submit(plane, 1)
        plane.tick()
        self.workers.down.add("w1")
        with patch("conch.ssh_control.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            plane.tick()
        self.workers.down.clear()
        time.sleep(0.1)
        self.pool._down.clear()  # host came back
        self.workers.calls.clear()
        plane.tick()
        self.assertEqual(len(self.workers.ops("task.events")), 1)


class ControllerHarness(PlaneHarness):
    def make_daemon(self, **kw):
        daemon = ControllerDaemon(
            {}, kernel_dir=self.root / "fleet",
            socket_path=self.root / "run" / "fleet.sock",
            transport_factory=self.factory, artifact_thread=False, **kw,
        )
        daemon.start()
        self.addCleanup(daemon.shutdown)
        # Point the harness at the daemon's own kernel.
        self.store = daemon.store
        self.registry = daemon.registry
        self.mission_id = ensure_adhoc_mission(daemon.store)
        return daemon


class TestControllerTick(ControllerHarness):
    def test_sweep_status_reused_for_skill_inventory(self):
        daemon = self.make_daemon()
        worker_id = self.enroll("w1", "host-1")
        daemon.tick()  # first tick sweeps
        self.assertEqual(len(self.workers.ops("worker.status")), 1)
        self.assertEqual(
            daemon.registry.worker_skills(daemon.registry.get(worker_id)),
            ["summarize"],
        )

    def test_completed_artifacts_queued_once_not_relisted(self):
        daemon = self.make_daemon()
        self.enroll("w1", "host-1")
        data = b"report bytes"
        digest = hashlib.sha256(data).hexdigest()
        self.workers.artifacts[digest] = data
        tasks = self.submit(daemon.plane, 1)
        daemon.tick()
        self.workers.complete(tasks[0], artifacts=[
            {"name": "report.txt", "digest": digest, "size": len(data)},
        ])
        original = daemon.store.list_dispatches
        succeeded_scans = []

        def counting(*args, **kwargs):
            if kwargs.get("state") == DispatchState.SUCCEEDED:
                succeeded_scans.append(1)
            return original(*args, **kwargs)

        with patch.object(daemon.store, "list_dispatches", counting):
            stats = daemon.tick()
            self.assertEqual(stats["artifacts_pending"], 1)
            self.assertEqual(daemon.artifacts.drain(), 1)
            for _ in range(3):
                daemon.tick()
        self.assertEqual(succeeded_scans, [])
        local = daemon.kernel_dir / "artifacts" / "sha256" / digest
        self.assertEqual(local.read_bytes(), data)
        self.assertEqual(daemon.artifacts.pending(), 0)

    def test_unpulled_artifacts_seeded_on_restart(self):
        daemon = self.make_daemon()
        self.enroll("w1", "host-1")
        data = b"left behind"
        digest = hashlib.sha256(data).hexdigest()
        self.workers.artifacts[digest] = data
        tasks = self.submit(daemon.plane, 1)
        daemon.tick()
        self.workers.complete(tasks[0], artifacts=[
            {"name": "a", "digest": digest, "size": len(data)},
        ])
        daemon.tick()
        daemon.shutdown()
        again = ControllerDaemon(
            {}, kernel_dir=self.root / "fleet",
            socket_path=self.root / "run" / "fleet.sock",
            transport_factory=self.factory, artifact_thread=False,
        )
        again.start()
        self.addCleanup(again.shutdown)
        self.assertEqual(again.artifacts.pending(), 1)
        self.assertEqual(again.artifacts.drain(), 1)


class TestArtifactPullerBackoff(PlaneHarness):
    def test_failed_pull_backs_off_instead_of_retrying_every_tick(self):
        worker_id = self.enroll("w1", "host-1")
        plane = TaskPlane(self.store, self.registry, self.factory)
        now = [0.0]
        puller = ArtifactPuller(
            plane, self.root / "store", lambda line: None,
            clock=lambda: now[0],
        )
        digest = "ab" * 32
        added = puller.add_dispatch(
            {"task_id": "t1", "result": {"artifacts": [
                {"name": "x", "digest": digest, "size": 3},
            ]}},
            self.registry.get(worker_id),
        )
        self.assertEqual(added, 1)
        # The worker does not have it: KeyError in the fake → failure.
        self.assertTrue(puller.process_one())
        calls = len(self.workers.ops("artifact.get"))
        self.assertFalse(puller.process_one())  # backing off
        self.assertEqual(len(self.workers.ops("artifact.get")), calls)
        now[0] += 6.0
        self.workers.artifacts[digest] = b"abc"
        plane._worker_backoff.clear()
        plane.begin_round()
        # Wrong bytes for the digest: refused, never stored.
        self.assertTrue(puller.process_one())
        self.assertFalse((self.root / "store" / digest).exists())
        self.assertEqual(list((self.root / "store").glob("*.part*")), [])

    def test_oversized_declared_artifact_not_queued(self):
        worker_id = self.enroll("w1", "host-1")
        plane = TaskPlane(self.store, self.registry, self.factory)
        puller = ArtifactPuller(plane, self.root / "store", lambda l: None)
        added = puller.add_dispatch(
            {"task_id": "t1", "result": {"artifacts": [
                {"name": "x", "digest": "cd" * 32, "size": 1 << 40},
            ]}},
            self.registry.get(worker_id),
        )
        self.assertEqual(added, 0)


def local_hostctl_popen(home):
    """Run the remote end of the ssh session (hostctl) locally."""
    def popen(argv, **kwargs):
        remote = shlex.split(argv[-1])
        assert remote[0] == "conch-hostctl", remote
        env = dict(os.environ)
        env["CONCH_FLEET_HOME"] = str(home)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.Popen(
            [sys.executable, "-m", "conch.fleet.hostctl", *remote[1:]],
            env=env, cwd=str(ROOT), **kwargs,
        )
    return popen


def old_hostctl_popen(argv, **kwargs):
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.stderr.write('conch-hostctl: error: unrecognized"
         " arguments: --worker w1\\n'); sys.exit(2)"],
        **kwargs,
    )


class TestStreamedArtifacts(PlaneHarness):
    def setUp(self):
        super().setUp()
        self.home = self.root / "fh"
        self.worker_id = self.enroll("w1", "host-1")
        self.popen = local_hostctl_popen(self.home)
        self.plane = TaskPlane(self.store, self.registry, self.stream_factory)

    def stream_factory(self, worker):
        return WorkerTransport(
            SSHTarget(host=worker["host"]), worker["name"],
            pool=self.pool, runner=self.workers.runner, popen=self.popen,
        )

    def put_on_worker(self, data, profile=""):
        digest = hashlib.sha256(data).hexdigest()
        base = self.home / "workers" / "w1"
        if profile:
            base.mkdir(parents=True, exist_ok=True)
            (base / "worker.json").write_text(json.dumps({
                "profile": profile,
            }))
            base = base / "state"
        store = base / "artifacts" / "sha256"
        store.mkdir(parents=True, exist_ok=True)
        (store / digest).write_bytes(data)
        return digest

    def test_pull_streams_once_and_verifies(self):
        data = os.urandom(700 * 1024)
        digest = self.put_on_worker(data)
        dest = self.root / "local" / digest
        size = self.plane.pull_artifact_to(
            self.registry.get(self.worker_id), digest, dest,
        )
        self.assertEqual(size, len(data))
        self.assertEqual(dest.read_bytes(), data)
        # One hostctl session, zero chunked RPCs.
        self.assertEqual(self.workers.ops("artifact.get"), [])
        self.assertEqual(list(dest.parent.glob("*.part*")), [])

    def test_docker_profile_reads_bind_mounted_store(self):
        data = b"from the container"
        digest = self.put_on_worker(data, profile="docker")
        dest = self.root / "local" / digest
        self.plane.pull_artifact_to(
            self.registry.get(self.worker_id), digest, dest,
        )
        self.assertEqual(dest.read_bytes(), data)

    def test_size_bound_enforced_while_streaming(self):
        data = os.urandom(300 * 1024)
        digest = self.put_on_worker(data)
        dest = self.root / "local" / digest
        with self.assertRaises(PlaneError):
            self.plane.pull_artifact_to(
                self.registry.get(self.worker_id), digest, dest,
                max_bytes=100 * 1024,
            )
        self.assertFalse(dest.exists())
        self.assertEqual(list(dest.parent.glob("*.part*")), [])

    def test_missing_artifact_is_a_refusal_not_a_connection_failure(self):
        dest = self.root / "local" / ("ef" * 32)
        with self.assertRaises(PlaneError) as ctx:
            self.plane.pull_artifact_to(
                self.registry.get(self.worker_id), "ef" * 32, dest,
            )
        self.assertIn("not in the store", str(ctx.exception))
        self.assertTrue(self.plane.worker_available(self.worker_id))

    def test_old_hostctl_falls_back_to_chunked_rpc(self):
        self.popen = old_hostctl_popen
        data = os.urandom(600 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        self.workers.artifacts[digest] = data
        dest = self.root / "local" / digest
        self.plane.pull_artifact_to(
            self.registry.get(self.worker_id), digest, dest,
        )
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual(len(self.workers.ops("artifact.get")), 3)
        # The fallback is remembered: no second streaming attempt.
        target = SSHTarget(host="host-1")
        self.assertFalse(self.pool.stream_supported(target))

    def test_push_streams_into_worker_store(self):
        data = os.urandom(400 * 1024)
        digest = hashlib.sha256(data).hexdigest()
        result = self.plane.push_artifact(
            self.registry.get(self.worker_id), digest, data,
        )
        self.assertTrue(result["complete"])
        stored = self.home / "workers" / "w1" / "artifacts" / "sha256"
        self.assertEqual((stored / digest).read_bytes(), data)

    def test_artifact_command_rejects_bad_digest(self):
        transport = self.stream_factory(self.registry.get(self.worker_id))
        with self.assertRaises(ArtifactTransferError):
            transport.artifact_command("get", "../../etc/passwd")


class TestPooledFactory(unittest.TestCase):
    def test_factory_shares_one_pool_and_closes_it(self):
        runtime = short_tmpdir(self) / "s"
        pool = SSHConnectionPool(SSHControlManager(runtime_dir=runtime))
        factory = PooledTransportFactory({}, pool=pool)
        worker = {"host": "h", "name": "w1", "labels": {}}
        self.assertIs(factory(worker).pool, factory(worker).pool)
        factory.close()
        with self.assertRaises(TransportError):
            pool.ensure(SSHTarget(host="h"))


if __name__ == "__main__":
    unittest.main()
