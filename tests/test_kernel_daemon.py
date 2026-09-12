"""Edge daemon gates (Swarm Phase 1).

Proven here: exclusive kernel ownership (OS lock + controller epoch), the
versioned control-socket protocol failing closed on version/op/shape, all
attach operations over the socket, idempotent tasks.json migration with the
original preserved as .bak, tick-driven timer→session→outbox flow with
delivered-or-queued notification records, log-transport delivery when no
channel is configured, and real-process graceful SIGTERM plus kill -9
restart/resume with no duplicate effects.
"""

import json
import os
import signal
import socket as socket_mod
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from conch.kernel import control
from conch.kernel.daemon import DaemonAlreadyRunning, EdgeDaemon
from conch.kernel.migrate import migrate_tasks_json
from conch.kernel.model import KernelError, MissionState


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def fake_factory(mission, messages, control_client, caps):
    return "fake session output", {"total_tokens": 7}


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.kernel_dir = self.root / "kernel"
        self.socket_path = self.root / "run" / "edge.sock"
        self.clock = FakeClock()
        self._daemons = []

    def make_daemon(self, config=None, notifier=None, clock=None):
        daemon = EdgeDaemon(
            config or {"provider": "openai"},
            kernel_dir=self.kernel_dir,
            state_dir=self.root,
            socket_path=self.socket_path,
            clock=clock or self.clock,
            session_factory=fake_factory,
            notifier=notifier,
        )
        self._daemons.append(daemon)
        self.addCleanup(daemon.shutdown)
        return daemon

    def call(self, op, args=None):
        return control.request(op, args, socket_path=self.socket_path)


class TestExclusiveOwnership(DaemonCase):
    def test_second_daemon_refused_by_os_lock(self):
        first = self.make_daemon()
        first.start()
        second = EdgeDaemon(
            {"provider": "openai"}, kernel_dir=self.kernel_dir,
            state_dir=self.root, socket_path=self.root / "run2" / "e.sock",
            clock=self.clock,
        )
        with self.assertRaises(DaemonAlreadyRunning):
            second.start()
        second.shutdown()
        # the first daemon still owns the kernel and serves requests
        self.assertTrue(control.daemon_alive(self.socket_path))

    def test_superseded_epoch_cannot_write(self):
        """A zombie that lost the lock (e.g. lock file removed by an
        operator) is fenced by the controller epoch even though it still
        holds open database handles."""
        zombie = self.make_daemon()
        zombie.start()
        mission_id = zombie.engine.create_mission(
            {"goal": "epoch test", "budgets": {}}, activate=False
        )
        # simulate losing the lock without releasing resources
        zombie._lock.release()
        successor = EdgeDaemon(
            {"provider": "openai"}, kernel_dir=self.kernel_dir,
            state_dir=self.root, socket_path=self.root / "run2" / "e.sock",
            clock=self.clock, session_factory=fake_factory,
        )
        self._daemons.append(successor)
        self.addCleanup(successor.shutdown)
        successor.start()
        self.assertGreater(successor.epoch, zombie.epoch)
        with self.assertRaises(KernelError):
            zombie.store.record_note(mission_id, "zombie write")
        successor.store.record_note(mission_id, "successor write")


class TestControlSocket(DaemonCase):
    def test_socket_permissions(self):
        daemon = self.make_daemon()
        daemon.start()
        directory_mode = os.stat(self.socket_path.parent).st_mode & 0o777
        socket_mode = os.stat(self.socket_path).st_mode & 0o777
        self.assertEqual(directory_mode, 0o700)
        self.assertEqual(socket_mode & 0o077, 0)

    def test_protocol_version_fails_closed(self):
        daemon = self.make_daemon()
        daemon.start()
        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(self.socket_path))
        sock.sendall(b'{"v": 99, "op": "status", "args": {}}\n')
        raw = sock.recv(65536)
        sock.close()
        response = json.loads(raw)
        self.assertFalse(response["ok"])
        self.assertIn("unsupported control protocol version",
                      response["error"])

    def test_malformed_and_unknown_requests_fail_closed(self):
        daemon = self.make_daemon()
        daemon.start()
        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(self.socket_path))
        sock.sendall(b"this is not json\n")
        response = json.loads(sock.recv(65536))
        sock.close()
        self.assertFalse(response["ok"])
        with self.assertRaises(control.ControlError):
            control.request(
                "warp.drive", socket_path=self.socket_path
            )

    def test_attach_operations_end_to_end(self):
        daemon = self.make_daemon()
        daemon.start()
        status = self.call("status")
        self.assertEqual(status["epoch"], 1)
        mission_id = self.call("mission.new", {"spec": {
            "goal": "daily digest", "budgets": {"sessions": 5},
            "cadence_seconds": 3600,
        }})["mission_id"]
        missions = self.call("missions.list")
        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0]["mission_id"], mission_id)
        self.assertEqual(missions[0]["status"], "ready")
        detail = self.call("mission.get", {"mission_id": mission_id})
        self.assertEqual(detail["spec"]["goal"], "daily digest")
        self.assertIn("budgets", detail)
        self.call("mission.pause", {"mission_id": mission_id})
        self.assertEqual(
            self.call("mission.get", {"mission_id": mission_id})["status"],
            "paused",
        )
        self.call("mission.resume", {"mission_id": mission_id})
        self.assertEqual(
            self.call("mission.get", {"mission_id": mission_id})["status"],
            "ready",
        )
        grant = daemon.store.request_approval(
            mission_id, "publish", {"x": 1}, ttl_seconds=600
        )
        approvals = self.call("approvals.list")
        self.assertEqual(len(approvals), 1)
        decided = self.call("approval.decide", {
            "approval_id": grant["approval_id"], "verb": "approve",
            "nonce": grant["nonce"],
        })
        self.assertEqual(decided["status"], "approved")
        self.call("mission.input", {
            "mission_id": mission_id, "text": "extra context",
        })
        self.call("mission.abort", {"mission_id": mission_id})
        self.assertEqual(
            self.call("mission.get", {"mission_id": mission_id})["status"],
            "cancelled",
        )

    def test_schedule_ops_and_legacy_ids(self):
        daemon = self.make_daemon()
        daemon.start()
        self.call("schedule.add", {"prompt": "check disk", "interval": 300})
        self.call("schedule.add", {
            "prompt": "rotate logs", "interval": 600, "run_once": True,
        })
        entries = self.call("schedule.list")
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry["task_seq"] > 0 for entry in entries))
        self.assertNotEqual(entries[0]["task_seq"], entries[1]["task_seq"])
        target = entries[0]
        self.call("schedule.cancel", {"mission_id": target["mission_id"]})
        remaining = [
            entry for entry in self.call("schedule.list") if entry["active"]
        ]
        self.assertEqual(len(remaining), 1)
        with self.assertRaises(control.ControlError):
            self.call("schedule.cancel", {"mission_id": "msn-none"})

    def test_responses_never_leak_environment_secrets(self):
        canary = "CANARY-edge-hunter2-never-log"
        os.environ["CONCH_TEST_SECRET"] = canary
        self.addCleanup(os.environ.pop, "CONCH_TEST_SECRET", None)
        daemon = self.make_daemon(config={
            "provider": "openai", "api_key_env": "CONCH_TEST_SECRET",
        })
        daemon.start()
        self.call("mission.new", {"spec": {
            "goal": "leak probe", "budgets": {},
        }})
        for op in ("status", "missions.list", "schedule.list",
                   "approvals.list"):
            self.assertNotIn(canary, json.dumps(self.call(op)))


class TestMigration(DaemonCase):
    def write_tasks(self, tasks):
        (self.root / "tasks.json").write_text(json.dumps(tasks))

    def test_tasks_json_migrates_idempotently_with_backup(self):
        now = time.time()
        self.write_tasks([
            {"id": 1, "prompt": "daily digest", "interval": 86400,
             "run_once": False, "active": True, "run_count": 12,
             "last_run": "2026-09-10 08:00:00", "next_run_at": now + 500},
            {"id": 2, "prompt": "one shot", "interval": 3600,
             "run_once": True, "active": True, "next_run_at": now - 50},
            {"id": 3, "prompt": "old stopped task", "interval": 60,
             "active": False},
        ])
        daemon = self.make_daemon(clock=time.time)
        daemon.start()
        entries = daemon.store.list_missions()
        self.assertEqual(len(entries), 2)  # inactive task skipped
        self.assertFalse((self.root / "tasks.json").exists())
        backup = self.root / "tasks.json.bak"
        self.assertTrue(backup.exists())
        original = json.loads(backup.read_text())
        self.assertEqual(len(original), 3)
        # preserved next-run time for the future-scheduled task
        digest = [
            mission for mission in entries
            if mission["spec"]["prompt"] == "daily digest"
        ][0]
        timer = daemon.store.find_timer(digest["mission_id"], "wake")
        self.assertAlmostEqual(timer["due_at"], now + 500, delta=2)
        # re-running the migration migrates nothing new
        report = migrate_tasks_json(daemon.engine, state_dir=self.root)
        self.assertEqual(report["migrated"], 0)
        # even if the same tasks.json reappears, digests dedupe it
        self.write_tasks([
            {"id": 1, "prompt": "daily digest", "interval": 86400,
             "run_once": False, "active": True},
        ])
        report = migrate_tasks_json(daemon.engine, state_dir=self.root)
        self.assertEqual(report["migrated"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(len(daemon.store.list_missions()), 2)

    def test_new_tasks_in_recreated_file_do_migrate(self):
        self.write_tasks([
            {"id": 1, "prompt": "first", "interval": 300, "active": True},
        ])
        daemon = self.make_daemon(clock=time.time)
        daemon.start()
        self.assertEqual(len(daemon.store.list_missions()), 1)
        self.write_tasks([
            {"id": 1, "prompt": "first", "interval": 300, "active": True},
            {"id": 2, "prompt": "second", "interval": 300, "active": True},
        ])
        report = migrate_tasks_json(daemon.engine, state_dir=self.root)
        self.assertEqual(report["migrated"], 1)
        self.assertEqual(len(daemon.store.list_missions()), 2)
        # both backups preserved
        backups = list(self.root.glob("tasks.json.bak*"))
        self.assertEqual(len(backups), 2)


class TestTickFlow(DaemonCase):
    def test_timer_session_outbox_flow_with_fake_channel(self):
        deliveries = []

        def notifier(payload):
            deliveries.append(payload)
            return True, "fake-channel", ""

        daemon = self.make_daemon(notifier=notifier)
        daemon.start()
        self.call("schedule.add", {"prompt": "check disk", "interval": 600})
        self.assertEqual(
            daemon.tick(), {"fired": 0, "sessions": 0, "delivered": 0}
        )
        self.clock.advance(601)
        stats = daemon.tick()
        self.assertEqual(stats["fired"], 1)
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["delivered"], 1)
        self.assertEqual(len(deliveries), 1)
        self.assertIn("fake session output", deliveries[0]["text"])
        delivered = daemon.store.list_outbox(status="delivered")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["transport"], "fake-channel")
        # the mission went back to waiting for its next cadence
        entries = self.call("schedule.list")
        self.assertEqual(entries[0]["status"], "waiting_timer")
        self.assertEqual(entries[0]["runs"], 1)

    def test_log_delivery_when_no_channel_configured(self):
        daemon = self.make_daemon(config={"provider": "openai"})
        daemon.start()
        self.call("schedule.add", {"prompt": "ping", "interval": 60})
        self.clock.advance(61)
        stats = daemon.tick()
        self.assertEqual(stats["delivered"], 1)
        delivered = daemon.store.list_outbox(status="delivered")
        self.assertEqual(delivered[0]["transport"], "log")
        log_text = daemon.log_path.read_text()
        self.assertIn("no channel configured", log_text)
        self.assertIn("fake session output", log_text)

    def test_failed_delivery_queues_with_backoff(self):
        def broken_notifier(payload):
            return False, "", "slack 500"

        daemon = self.make_daemon(notifier=broken_notifier)
        daemon.start()
        daemon.store.enqueue_outbox(
            "channel_notify", {"text": "hello"}, "k1"
        )
        daemon.tick()
        pending = daemon.store.list_outbox(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertIn("slack 500", pending[0]["last_error"])
        # the queued record survives for the operator to inspect
        self.assertIn("will retry", daemon.log_path.read_text())

    def test_unknown_outbox_kind_parked_as_failed(self):
        daemon = self.make_daemon()
        daemon.start()
        daemon.store.enqueue_outbox("teleport", {"x": 1}, "k1")
        daemon.tick()
        failed = daemon.store.list_outbox(status="failed")
        self.assertEqual(len(failed), 1)

    def test_restart_resumes_abandoned_session_without_duplicates(self):
        """In-process kill -9 equivalence at the session boundary."""
        daemon = self.make_daemon()
        daemon.start()
        self.call("schedule.add", {"prompt": "work", "interval": 600})
        self.clock.advance(601)
        for claim in daemon.store.claim_due_timers(daemon.holder):
            daemon.store.fire_timer(
                claim["timer_id"], claim["generation"], holder=daemon.holder
            )
        mission = daemon.store.list_missions()[0]
        daemon.store.start_session(
            mission["mission_id"], "ses-crash", daemon.holder, {},
            lease_seconds=120,
        )
        # kill -9: no shutdown, no checkpoint — just drop everything
        daemon._lock.release()
        daemon._stop.set()
        daemon._server.close()
        daemon.store.close()
        successor = self.make_daemon()
        self.clock.advance(121)  # session lease expires
        successor.start()  # reconcile runs at start
        mission = successor.store.get_mission(mission["mission_id"])
        self.assertEqual(mission["status"], MissionState.READY)
        stats = successor.tick()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["fired"], 0)  # the old fire never repeats
        mission = successor.store.get_mission(mission["mission_id"])
        self.assertEqual(mission["runs"], 1)
        fired = [
            event for event in successor.store.event_tail(
                mission["mission_id"], limit=1000
            )
            if event["kind"] == "timer_fired"
        ]
        self.assertEqual(len(fired), 1)
        ok, detail = successor.store.replay_matches_live()
        self.assertTrue(ok, detail)


class TestRealProcessLifecycle(unittest.TestCase):
    """Graceful SIGTERM and kill -9 restart against a real daemon process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.env = dict(os.environ)
        self.env.update({
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "CONCH_EDGE_DAEMON": "true",
            "CONCH_PROVIDER": "openai",
        })
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / "runtime", 0o700)
        self.socket_path = self.root / "runtime" / "conch" / "edge.sock"

    def spawn(self):
        process = subprocess.Popen(
            [sys.executable, "-c",
             "from conch.entrypoints import edge_main; import sys;"
             " sys.exit(edge_main([]))"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.addCleanup(process.stdout.close)
        deadline = time.time() + 30
        while time.time() < deadline:
            if control.daemon_alive(self.socket_path):
                return process
            if process.poll() is not None:
                output = process.stdout.read().decode()
                self.fail(f"daemon exited early: {output}")
            time.sleep(0.2)
        process.kill()
        self.fail("daemon did not come up within 30s")

    def test_sigterm_is_graceful_and_kill9_resumes(self):
        process = self.spawn()
        try:
            mission_id = control.request("mission.new", {"spec": {
                "goal": "lifecycle proof", "budgets": {},
                "cadence_seconds": 3600,
            }}, socket_path=self.socket_path)["mission_id"]
            # --- graceful SIGTERM ---
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=30), 0)
            self.assertFalse(self.socket_path.exists())
            # --- restart after SIGTERM: state intact ---
            process = self.spawn()
            missions = control.request(
                "missions.list", socket_path=self.socket_path
            )
            self.assertEqual(missions[0]["mission_id"], mission_id)
            # --- kill -9 mid-flight ---
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=30)
            # --- restart after kill -9: lock free, state intact ---
            process = self.spawn()
            missions = control.request(
                "missions.list", socket_path=self.socket_path
            )
            self.assertEqual(missions[0]["mission_id"], mission_id)
            status = control.request(
                "status", socket_path=self.socket_path
            )
            self.assertEqual(status["epoch"], 3)  # one adoption per start
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=30), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    def test_edge_disabled_exits_with_config_error(self):
        env = dict(self.env)
        env.pop("CONCH_EDGE_DAEMON")
        result = subprocess.run(
            [sys.executable, "-c",
             "from conch.entrypoints import edge_main; import sys;"
             " sys.exit(edge_main([]))"],
            env=env, capture_output=True, text=True, timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("edge_daemon", result.stderr)


class TestKernelClientAttach(DaemonCase):
    def test_socket_client_when_daemon_runs_direct_when_not(self):
        from conch.kernel.client import attach_kernel

        daemon = self.make_daemon()
        daemon.start()
        client = attach_kernel(
            {"provider": "openai"}, socket_path=self.socket_path,
            kernel_dir=self.kernel_dir,
        )
        self.assertEqual(client.mode, "socket")
        mission_id = client.new_mission({"goal": "attach test",
                                         "budgets": {}})
        client.close()
        daemon.shutdown()
        client = attach_kernel(
            {"provider": "openai"}, socket_path=self.socket_path,
            kernel_dir=self.kernel_dir,
        )
        try:
            self.assertEqual(client.mode, "direct")
            missions = client.list_missions()
            self.assertEqual(missions[0]["mission_id"], mission_id)
            # identical shapes across transports
            detail = client.get_mission(mission_id)
            self.assertIn("budgets", detail)
            self.assertIn("task_seq", detail)
        finally:
            client.close()

    def test_scheduler_adapter_matches_legacy_surface(self):
        from conch.kernel.client import KernelSchedulerAdapter

        daemon = self.make_daemon()
        daemon.start()
        adapter = KernelSchedulerAdapter(
            {"provider": "openai"}, socket_path=self.socket_path,
            kernel_dir=self.kernel_dir,
        )
        self.addCleanup(adapter.stop)
        self.assertTrue(adapter.daemon_running())
        task = adapter.add("check disk", 300)
        self.assertGreater(task.id, 0)
        self.assertEqual(task.prompt, "check disk")
        self.assertEqual(task.interval, 300)
        self.assertTrue(task.active)
        tasks = adapter.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, task.id)
        self.assertTrue(adapter.cancel(task.id))
        # legacy Scheduler.cancel returns True whenever the id exists,
        # active or not — the adapter matches that
        self.assertTrue(adapter.cancel(task.id))
        self.assertFalse(adapter.cancel(99999))
        self.assertFalse(
            any(task.active for task in adapter.list_tasks())
        )


if __name__ == "__main__":
    unittest.main()
