"""WorkerTransport argv/wire contract and the enrollment flow.

No remote SSH host is reachable in this environment, so both are exercised
through injected runners: the transport wire contract against a real local
worker socket, and enrollment against a scripted runner that models a bare
host. Real-host verification is a documented follow-up.
"""

import json
import tempfile
import unittest
from pathlib import Path

from conch.fleet.enroll import (
    Enroller,
    EnrollmentError,
    build_ssh_target,
    hostctl_digest,
)
from conch.fleet.registry import FleetRegistry
from conch.fleet.transport import TransportError, WorkerTransport
from conch.kernel.store import MissionStore
from conch.ssh_control import SSHControlManager, SSHTarget
from conch.swarm.protocol import RpcRequest, RpcResponse, new_id

from tests.fleet_fakes import FakeSSHWorkerTransport, LocalWorkerProcess


class TestWorkerTransportArgv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = SSHControlManager(
            runtime_dir=Path(self._tmp.name) / "ssh"
        )
        self.target = SSHTarget(host="fleet-host", user="conch")

    def test_argv_has_no_secrets_and_uses_stdio_relay(self):
        transport = WorkerTransport(
            self.target, "box1", manager=self.manager
        )
        argv = transport.argv()
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("conch-hostctl rpc --worker box1", argv)
        joined = " ".join(argv)
        self.assertNotIn("password", joined.lower())
        self.assertNotIn("sshpass", joined.lower())

    def test_send_parses_response_and_matches_id(self):
        captured = {}

        def runner(argv, stdin_bytes, timeout):
            captured["argv"] = argv
            captured["stdin"] = stdin_bytes
            request = RpcRequest.from_json(stdin_bytes.strip())
            reply = RpcResponse(
                rpc_id=request.rpc_id, ok=True, result={"pong": True},
            )
            return 0, reply.to_json().encode() + b"\n", b""

        transport = WorkerTransport(
            self.target, "box1", manager=self.manager, runner=runner,
        )
        resp = transport.call("worker.status")
        self.assertTrue(resp.ok)
        self.assertEqual(resp.result, {"pong": True})
        # The request travelled on stdin, never argv.
        self.assertIn(b"worker.status", captured["stdin"])
        self.assertFalse(
            any("worker.status" in a and a != transport.remote_command()
                for a in captured["argv"])
        )

    def test_mismatched_response_id_rejected(self):
        def runner(argv, stdin_bytes, timeout):
            reply = RpcResponse(rpc_id=new_id("rpc"), ok=True)
            return 0, reply.to_json().encode() + b"\n", b""

        transport = WorkerTransport(
            self.target, "box1", manager=self.manager, runner=runner,
        )
        with self.assertRaises(TransportError):
            transport.call("worker.status")

    def test_empty_output_is_a_transport_error(self):
        def runner(argv, stdin_bytes, timeout):
            return 255, b"", b"ssh: connect to host timed out"

        transport = WorkerTransport(
            self.target, "box1", manager=self.manager, runner=runner,
        )
        with self.assertRaises(TransportError) as ctx:
            transport.call("worker.status")
        self.assertIn("timed out", str(ctx.exception))

    def test_transport_against_real_worker_socket(self):
        """End-to-end over the real supervisor socket via a runner that
        connects to it — the wire contract, exercised without SSH."""
        worker = LocalWorkerProcess(Path(self._tmp.name) / "worker").start()
        self.addCleanup(worker.stop)

        def runner(argv, stdin_bytes, timeout):
            raw = worker.rpc_raw(json.loads(stdin_bytes))
            return 0, raw, b""

        transport = WorkerTransport(
            SSHTarget(host="local"), "box1", manager=self.manager,
            runner=runner,
        )
        resp = transport.call("worker.status")
        self.assertTrue(resp.ok)
        self.assertEqual(resp.result["queue"]["in_flight"], 0)


class EnrollCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MissionStore(Path(self._tmp.name) / "kernel.db")
        self.addCleanup(self.store.close)
        self.registry = FleetRegistry(self.store)
        self.manager = SSHControlManager(
            runtime_dir=Path(self._tmp.name) / "ssh"
        )

    def _probe_json(self, profiles=("systemd", "process")):
        return json.dumps({
            "ok": True, "op": "probe", "os": "linux", "arch": "x86_64",
            "python": {"version": "3.11.2", "executable": "/usr/bin/python3"},
            "systemd": {"present": True, "version": 252},
            "docker": {"present": False}, "disk": {"total": 1, "free": 1},
            "cgroups": "v2", "gpu": {"present": False, "gpus": []},
            "model_endpoints": [], "profiles": list(profiles),
            "hostname": "fleet-host",
        }).encode()

    def _make_runner(self, autonomy=True, probe_ok=True):
        digest = hostctl_digest()
        calls = []

        def runner(argv, stdin_bytes, timeout):
            joined = " ".join(argv)
            calls.append(joined)
            if argv[-1] == "true":
                return (0 if autonomy else 255), b"", (
                    b"" if autonomy else b"Permission denied (publickey)."
                )
            if "cat >" in joined:
                # staging the streamed hostctl bytes
                assert stdin_bytes  # the file content travels on stdin
                return 0, b"", b""
            if "install-verify" in joined:
                return 0, json.dumps({
                    "ok": True, "op": "install-verify", "digest": digest,
                    "installed": "/home/conch/.local/state/conch-fleet/"
                                 "hostctl.py", "home": "x",
                    "hostctl_version": 1,
                }).encode(), b""
            if "probe" in joined:
                if not probe_ok:
                    return 1, b"", b"probe boom"
                return 0, self._probe_json(), b""
            if "trust-install" in joined:
                return 0, json.dumps({
                    "ok": True, "op": "trust-install", "sha256": "0" * 64,
                    "path": "x",
                }).encode(), b""
            return 0, b"{}", b""

        self.calls = calls
        return runner

    def test_enroll_autonomous_host(self):
        enroller = Enroller(
            self.registry, manager=self.manager,
            runner=self._make_runner(autonomy=True),
        )
        target = build_ssh_target("fleet-host", user="conch")
        receipt = enroller.enroll(
            "box1", target, trust_level=3, resource_group="ollama-1",
            allowed_signers_bytes=b"principal namespaces=... ssh-ed25519 X\n",
            interactive_first=False,
        )
        self.assertTrue(receipt["autonomy_capable"])
        self.assertEqual(receipt["runtime_profile"], "systemd")
        worker = self.registry.find("box1")
        self.assertIsNotNone(worker)
        self.assertTrue(worker["autonomy_capable"])
        self.assertEqual(worker["trust_level"], 3)
        self.assertEqual(worker["resource_group"], "ollama-1")
        self.assertEqual(worker["capabilities"]["arch"], "x86_64")
        self.assertEqual(worker["profiles"], ["systemd", "process"])
        self.assertIsNotNone(receipt["trust"])

    def test_password_only_host_enrolls_but_not_autonomous(self):
        enroller = Enroller(
            self.registry, manager=self.manager,
            runner=self._make_runner(autonomy=False),
        )
        target = build_ssh_target("fleet-host", user="conch")
        receipt = enroller.enroll("box2", target, interactive_first=False)
        self.assertFalse(receipt["autonomy_capable"])
        worker = self.registry.find("box2")
        self.assertFalse(worker["autonomy_capable"])

    def test_bootstrap_streams_bytes_on_stdin_not_argv(self):
        enroller = Enroller(
            self.registry, manager=self.manager,
            runner=self._make_runner(),
        )
        target = build_ssh_target("fleet-host", user="conch")
        enroller.enroll("box3", target, interactive_first=False)
        # No call carries the hostctl source or a digest secret in a way
        # that would place raw file bytes in argv.
        for joined in self.calls:
            self.assertNotIn("def cmd_probe", joined)

    def test_probe_failure_aborts_enrollment(self):
        enroller = Enroller(
            self.registry, manager=self.manager,
            runner=self._make_runner(probe_ok=False),
        )
        target = build_ssh_target("fleet-host", user="conch")
        with self.assertRaises(EnrollmentError):
            enroller.enroll("box4", target, interactive_first=False)
        self.assertIsNone(self.registry.find("box4"))

    def test_duplicate_name_refused(self):
        enroller = Enroller(
            self.registry, manager=self.manager,
            runner=self._make_runner(),
        )
        target = build_ssh_target("fleet-host", user="conch")
        enroller.enroll("box5", target, interactive_first=False)
        with self.assertRaises(EnrollmentError):
            enroller.enroll("box5", target, interactive_first=False)

    def test_interactive_confirmation_gate(self):
        refusals = []

        def confirm(prompt):
            refusals.append(prompt)
            return False

        enroller = Enroller(
            self.registry, manager=self.manager,
            runner=self._make_runner(), confirm=confirm,
        )
        target = build_ssh_target("fleet-host", user="conch")
        with self.assertRaises(EnrollmentError):
            enroller.enroll("box6", target, interactive_first=True)
        self.assertEqual(len(refusals), 1)

    def test_autonomy_check_argv_is_fresh_batchmode(self):
        enroller = Enroller(self.registry, manager=self.manager)
        argv = enroller._autonomy_argv(
            build_ssh_target("fleet-host", user="conch", port=2222)
        )
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ControlPath=none", argv)
        self.assertIn("ControlMaster=no", argv)
        self.assertEqual(argv[-1], "true")
        self.assertIn("2222", argv)


class TestTransportPlusWorkerParity(unittest.TestCase):
    """The FakeSSHWorkerTransport used by plane tests is a faithful stand-in
    for WorkerTransport: both send RpcRequest and return RpcResponse."""

    def test_fake_transport_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = LocalWorkerProcess(Path(tmp) / "w").start()
            try:
                fake = FakeSSHWorkerTransport(worker)
                resp = fake.send(RpcRequest(
                    rpc_id=new_id("rpc"), op="worker.status", args={},
                ))
                self.assertIsInstance(resp, RpcResponse)
                self.assertTrue(resp.ok)
            finally:
                worker.stop()


if __name__ == "__main__":
    unittest.main()
