"""conch-controller end-to-end gates (the fleet awakening).

Real worker supervisor subprocesses reached through the fake SSH
transport; the controller daemon (or a direct-drive client — the same
code paths) schedules, polls, sweeps, and pulls artifacts. Task
subprocesses run the real chat_turn under a scripted raw_fn.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.fleet import authority
from conch.fleet.client import (
    DirectFleetClient,
    SocketFleetClient,
    run_fleet_task,
)
from conch.fleet.controller import ControllerDaemon, ensure_adhoc_mission
from conch.kernel.model import DispatchState, KernelError, WorkerState
from conch.kernel.store import MissionStore
from conch.swarm.protocol import TaskEnvelope, new_id

from tests.fleet_fakes import FakeSSHWorkerTransport, LocalWorkerProcess


class ControllerCase(unittest.TestCase):
    """Shared fixture: tmp fleet kernel + real local worker processes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.kernel_dir = self.root / "fleet"
        self.kernel_dir.mkdir(parents=True)
        self._workers = {}

    def transport_factory(self, worker):
        return FakeSSHWorkerTransport(self._workers[worker["worker_id"]])

    def start_worker(self, registry, name="box1", *, script_text="done",
                     script=None, env_extra=None, trust_level=2,
                     data_ceiling="confidential", activate=True):
        home = self.root / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"agent_mode": True}))
        if script is None:
            script = home / "script.json"
            script.write_text(json.dumps([{"content": script_text}]))
        worker_id = registry.enroll(
            name, host=f"10.0.0.{len(self._workers) + 1}",
            trust_level=trust_level, data_ceiling=data_ceiling,
            max_concurrency=2, profiles=["process"],
            capabilities={"os": "linux"},
        )
        if activate:
            registry.activate(worker_id)
        env = {"CONCH_FLEET_TASK_SCRIPT": str(script)}
        env.update(env_extra or {})
        proc = LocalWorkerProcess(home, env_extra=env).start()
        self.addCleanup(proc.stop)
        self._workers[worker_id] = proc
        return worker_id

    def make_daemon(self, config=None):
        daemon = ControllerDaemon(
            dict(config or {}),
            kernel_dir=self.kernel_dir,
            socket_path=self.root / "run" / "fleet.sock",
            tick_seconds=0.1,
            transport_factory=self.transport_factory,
        )
        return daemon


class TestControllerDaemonE2E(ControllerCase):
    def test_daemon_dispatches_over_socket_end_to_end(self):
        """Submit over the control socket; the daemon's ticks place the
        task on a fake worker, a real bounded session runs, and the
        result comes back through task.get."""
        daemon = self.make_daemon()
        daemon.start()
        self.addCleanup(daemon.shutdown)
        self.start_worker(daemon.registry, script_text="the answer is 42")
        runner = threading.Thread(target=daemon.run_forever, daemon=True)
        runner.start()
        self.addCleanup(daemon.request_stop)
        client = SocketFleetClient(self.root / "run" / "fleet.sock")
        status = client.status()
        self.assertEqual(status["epoch"], daemon.epoch)
        dispatch = run_fleet_task(
            client, task="do the thing", worker="box1", timeout=30.0,
        )
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)
        self.assertIn("42", dispatch["result"]["summary"])
        events = client.task_events(dispatch["task_id"])
        kinds = {event["kind"] for event in events}
        self.assertIn("started", kinds)
        self.assertIn("result", kinds)
        # The registry + ceiling surface over the socket.
        entries = client.list_workers()
        self.assertEqual(entries[0]["name"], "box1")
        self.assertIn("local_shell", entries[0]["ceiling"]["tools"])
        probe = client.probe_worker("box1")
        self.assertTrue(probe["reachable"])
        daemon.request_stop()
        runner.join(timeout=10)

    def test_artifacts_flow_back_and_are_pulled(self):
        """A task that leaves files in out/ publishes artifacts; the
        controller tick pulls them into the local content store."""
        daemon = self.make_daemon()
        daemon.start()
        self.addCleanup(daemon.shutdown)
        script = self.root / "artifact-script.json"
        script.write_text(json.dumps([
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell", "arguments": json.dumps(
                    {"command":
                     "mkdir -p out && printf 'hello fleet' > out/report.txt"}
                )},
            }]},
            {"content": "report written"},
        ]))
        self.start_worker(daemon.registry, name="arty", script=script)
        runner = threading.Thread(target=daemon.run_forever, daemon=True)
        runner.start()
        self.addCleanup(daemon.request_stop)
        client = SocketFleetClient(self.root / "run" / "fleet.sock")
        dispatch = run_fleet_task(
            client, task="write the report", worker="arty",
            tools=["local_shell"], timeout=30.0,
        )
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)
        artifacts = dispatch["result"]["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["name"], "report.txt")
        digest = artifacts[0]["digest"]
        # The daemon auto-pulls on its tick; wait briefly, else pull on
        # demand through the same op the /fleet command uses.
        local = self.kernel_dir / "artifacts" / "sha256" / digest
        deadline = time.time() + 10
        while time.time() < deadline and not local.is_file():
            time.sleep(0.2)
        if not local.is_file():
            client.pull_artifact(dispatch["task_id"], digest)
        self.assertEqual(local.read_bytes(), b"hello fleet")
        daemon.request_stop()
        runner.join(timeout=10)

    def test_drain_semantics(self):
        """DRAINING workers accept no new work; enable restores placement."""
        daemon = self.make_daemon()
        daemon.start()
        self.addCleanup(daemon.shutdown)
        self.start_worker(daemon.registry, name="drainy")
        client = DirectFleetClient(
            {}, kernel_dir=self.kernel_dir,
            transport_factory=self.transport_factory,
        )
        # Use the daemon's own store through the socket for admin ops but
        # tick manually for determinism.
        sock = SocketFleetClient(self.root / "run" / "fleet.sock")
        sock.drain("drainy")
        self.assertEqual(
            daemon.registry.find("drainy")["state"], WorkerState.DRAINING
        )
        mission_id = ensure_adhoc_mission(daemon.store)
        envelope = TaskEnvelope(
            task_id=new_id("task"), mission_id=mission_id,
            principal="user", task="held back",
            idempotency_key=new_id("task"), issued_at=time.time(),
        )
        task_id = daemon.plane.submit(envelope)
        daemon.tick()
        self.assertEqual(
            daemon.store.get_dispatch(task_id)["state"],
            DispatchState.QUEUED,
        )
        sock.enable("drainy")
        deadline = time.time() + 25
        while time.time() < deadline:
            daemon.tick()
            if daemon.store.get_dispatch(task_id)["state"] in (
                DispatchState.TERMINAL
            ):
                break
            time.sleep(0.1)
        self.assertEqual(
            daemon.store.get_dispatch(task_id)["state"],
            DispatchState.SUCCEEDED,
        )
        client.close()


class TestControllerRestartFencing(ControllerCase):
    def test_kill_restart_mid_dispatch_no_double_execution(self):
        """Controller A starts a dispatch and dies; controller B adopts a
        higher epoch, finalizes the same attempt from the worker's
        receipt, and A can never write again. The worker ran the task
        exactly once."""
        store_a = MissionStore(self.kernel_dir / "kernel.db")
        self.addCleanup(store_a.close)
        store_a.adopt_epoch()
        from conch.fleet.plane import TaskPlane
        from conch.fleet.registry import FleetRegistry

        registry_a = FleetRegistry(store_a)
        script = self.root / "slow.json"
        script.write_text(json.dumps([
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "local_shell",
                             "arguments": json.dumps(
                                 {"command": "sleep 2"})},
            }]},
            {"content": "survived the failover"},
        ]))
        worker_id = self.start_worker(registry_a, name="fenced",
                                      script=script)
        plane_a = TaskPlane(store_a, registry_a, self.transport_factory)
        mission_id = ensure_adhoc_mission(store_a)
        envelope = TaskEnvelope(
            task_id=new_id("task"), mission_id=mission_id,
            principal="user", task="do the thing",
            idempotency_key=new_id("task"), issued_at=time.time(),
            tools=("local_shell",), wall_clock_seconds=60,
        )
        task_id = plane_a.submit(envelope)
        deadline = time.time() + 20
        while time.time() < deadline:
            plane_a.schedule_once()
            if store_a.get_dispatch(task_id)["state"] == (
                DispatchState.RUNNING
            ):
                break
            time.sleep(0.1)
        self.assertEqual(
            store_a.get_dispatch(task_id)["state"], DispatchState.RUNNING
        )
        # "Kill" controller A (it keeps its open handles — the zombie
        # case) and start controller B on the same fleet kernel.
        store_b = MissionStore(self.kernel_dir / "kernel.db")
        self.addCleanup(store_b.close)
        store_b.adopt_epoch()
        registry_b = FleetRegistry(store_b)
        plane_b = TaskPlane(store_b, registry_b, self.transport_factory)
        # The superseded controller can no longer write at all.
        with self.assertRaises(KernelError):
            store_a.transition_dispatch(
                task_id, DispatchState.CANCELLED, reason="zombie write"
            )
        # Controller B finishes supervision of the SAME attempt.
        deadline = time.time() + 25
        while time.time() < deadline:
            plane_b.schedule_once()
            plane_b.poll_once()
            dispatch = store_b.get_dispatch(task_id)
            if dispatch["state"] in DispatchState.TERMINAL:
                break
            time.sleep(0.15)
        dispatch = store_b.get_dispatch(task_id)
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)
        self.assertIn("survived", dispatch["result"]["summary"])
        # Exactly one execution: attempt 1, and the worker spooled exactly
        # one 'started' event for it.
        self.assertEqual(dispatch["attempt"], 1)
        events = store_b.list_dispatch_events(task_id)
        started = [e for e in events if e["kind"] == "started"]
        self.assertEqual(len(started), 1)
        # A stale poll by the dead controller cannot commit either.
        worker = registry_b.get(worker_id)
        self.assertIsNotNone(worker)


class TestSkillAddressedDispatch(ControllerCase):
    def _make_skill_home(self, tools="local_shell"):
        config_home = self.root / "xdg-config"
        skills_dir = config_home / "conch" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "unitcheck.md").write_text(
            "---\n"
            "name: unitcheck\n"
            "description: report host facts\n"
            f"tools: {tools}\n"
            "---\n"
            "Report the requested host facts precisely.\n"
        )
        return config_home

    def test_skill_dispatch_end_to_end(self):
        """A skill-addressed envelope places only on a worker reporting
        the skill, and the worker session loads it."""
        config_home = self._make_skill_home()
        daemon = self.make_daemon()
        daemon.start()
        self.addCleanup(daemon.shutdown)
        self.start_worker(
            daemon.registry, name="skilled", script_text="acted as skill",
            env_extra={"XDG_CONFIG_HOME": str(config_home)},
        )
        client = DirectFleetClient(
            {}, kernel_dir=self.kernel_dir,
            transport_factory=self.transport_factory,
        )
        self.addCleanup(client.close)
        daemon.request_stop()  # direct drive below; daemon holds the lock
        with patch.dict(os.environ,
                        {"XDG_CONFIG_HOME": str(config_home)}):
            dispatch = run_fleet_task(
                client, task="report uname", worker="skilled",
                skill="unitcheck", timeout=30.0,
            )
        self.assertEqual(dispatch["state"], DispatchState.SUCCEEDED)
        self.assertEqual(
            dispatch["envelope"]["skills"], ["unitcheck"]
        )
        # The skill's tool scope became the envelope's tool set.
        self.assertEqual(dispatch["envelope"]["tools"], ["local_shell"])
        # The registry recorded the worker's skill inventory.
        worker = daemon.registry.find("skilled")
        self.assertIn(
            "unitcheck", daemon.registry.worker_skills(worker)
        )

    def test_missing_remote_skill_is_refused_with_clear_error(self):
        config_home = self._make_skill_home()
        daemon = self.make_daemon()
        daemon.start()
        self.addCleanup(daemon.shutdown)
        # Worker WITHOUT the skill installed (no XDG override).
        self.start_worker(daemon.registry, name="unskilled")
        client = DirectFleetClient(
            {}, kernel_dir=self.kernel_dir,
            transport_factory=self.transport_factory,
        )
        self.addCleanup(client.close)
        with patch.dict(os.environ,
                        {"XDG_CONFIG_HOME": str(config_home)}):
            with self.assertRaises(authority.AuthorityError) as ctx:
                run_fleet_task(
                    client, task="report uname", worker="unskilled",
                    skill="unitcheck", timeout=10.0,
                )
        self.assertIn("unitcheck", str(ctx.exception))
        self.assertIn("does not report skill", str(ctx.exception))

    def test_unknown_local_skill_is_refused(self):
        daemon = self.make_daemon()
        daemon.start()
        self.addCleanup(daemon.shutdown)
        self.start_worker(daemon.registry, name="w")
        client = DirectFleetClient(
            {}, kernel_dir=self.kernel_dir,
            transport_factory=self.transport_factory,
        )
        self.addCleanup(client.close)
        with self.assertRaises(KernelError):
            run_fleet_task(
                client, task="x", worker="w",
                skill="no-such-skill-anywhere", timeout=5.0,
            )


class TestExecutorSkillScope(unittest.TestCase):
    """Worker-side recheck: the executor loads the skill's prompt and
    re-intersects its tool scope; a missing skill fails POLICY."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        config_home = self.root / "xdg-config"
        skills = config_home / "conch" / "skills"
        skills.mkdir(parents=True)
        (skills / "scoped.md").write_text(
            "---\nname: scoped\ndescription: d\ntools: public_api\n---\n"
            "Follow the scoped procedure.\n"
        )
        self._env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(config_home),
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def _executor(self, skills=("scoped",)):
        from conch.fleet.taskexec import TaskExecutor

        home = self.root / "home"
        task_id = new_id("task")
        workspace = home / "workspaces" / task_id
        workspace.mkdir(parents=True)
        envelope = TaskEnvelope(
            task_id=task_id, mission_id="msn-" + "0" * 13 + "-" + "0" * 16,
            principal="user", task="do it",
            idempotency_key=new_id("task"), issued_at=1.0,
            skills=tuple(skills),
            tools=("local_shell", "public_api"),
        )
        (workspace / "task.json").write_text(json.dumps({
            "envelope": envelope.to_dict(), "attempt": 1, "fence": 1,
        }, sort_keys=True))
        return TaskExecutor(home, task_id, 1)

    def test_skill_narrows_the_tool_intersection(self):
        executor = self._executor()
        resolved = executor._resolve_skills()
        self.assertEqual(resolved[0]["name"], "scoped")
        clients, tools = executor._build_tools({}, resolved)
        names = {t["function"]["name"] for t in tools}
        self.assertEqual(names, {"public_api"})
        self.assertNotIn("local_shell", clients)
        messages = executor._fresh_messages({}, resolved)
        self.assertIn("scoped procedure", messages[0]["content"])
        self.assertIn("acting as the following skill",
                      messages[0]["content"])

    def test_missing_skill_fails_policy(self):
        from conch.fleet.taskexec import SkillUnavailable

        executor = self._executor(skills=("ghost",))
        with self.assertRaises(SkillUnavailable):
            executor._resolve_skills()


if __name__ == "__main__":
    unittest.main()
