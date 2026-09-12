"""Daemon-hosted channel intake (always-on daemon work item).

Proven here: the edge daemon answers inbound channel messages with full
agent turns and no interactive shell attached; every remote-safety
invariant holds in the daemon exactly as in the shell (fail-closed
allowlists, safe_auto cap via RemoteShellClient, excluded tools, bounded
replies, thread == conversation); `input msn-… <text>` wakes the addressed
mission and its session fires in the same tick; and the kernel
``channel_intake`` lease guarantees exactly one channel consumer in both
directions — shell first, daemon first — with immediate handoff on release
and epoch fencing for superseded daemons. The fake loopback channel used
throughout enforces the same allowlist/cursor semantics as the real
transports.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.bootstrap import _ShellIntakeGate, start_remote_loop
from conch.channels import ChannelManager, FakeChannel
from conch.kernel import control
from conch.kernel.daemon import EdgeDaemon
from conch.kernel.intake import (
    INTAKE_LEASE_KIND,
    INTAKE_LEASE_RESOURCE,
    intake_lease_seconds,
)
from conch.kernel.model import MissionState
from conch.remote import REMOTE_EXCLUDED_TOOLS, RemoteLoop, RemoteShellClient
from conch.tooling import ToolRuntimeState, set_agent_mode


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def plain_factory(mission, messages, control_client, caps):
    return "mission session output", {"total_tokens": 3}


def input_requesting_factory():
    calls = {"n": 0}

    def factory(mission, messages, control_client, caps):
        calls["n"] += 1
        if calls["n"] == 1:
            control_client.call_tool(
                "mission_control",
                {"op": "request_input", "text": "which repo?"},
            )
        return f"run {calls['n']}", {"total_tokens": 3}

    factory.calls = calls
    return factory


class IntakeCase(unittest.TestCase):
    """Isolated XDG + a daemon on the default (XDG-derived) paths, so the
    control socket, kernel database, and channel cursors line up exactly
    the way attach_kernel and the shell-side gate expect."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / "runtime", 0o700)
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)
        # Host sessions must never spawn the user's real MCP servers or
        # touch the real tool cache (conch.mcp resolves its paths at import
        # time, before this fixture's XDG isolation applies).
        for target, replacement in (
            ("create_clients", lambda: {}),
            ("collect_tools", lambda clients: ([], {})),
            ("save_tool_cache", lambda tools: None),
        ):
            mcp_patcher = patch(f"conch.mcp.{target}", replacement)
            mcp_patcher.start()
            self.addCleanup(mcp_patcher.stop)
        self.chan_dir = self.root / "chan"
        self.clock = FakeClock()

    def base_config(self, **overrides):
        config = {
            "provider": "ollama",
            "chat_model": "qwen3:8b", "model": "qwen3:8b",
            "remote_enabled": "true",
            "remote_poll_interval": 1,
            "fake_channel_dir": str(self.chan_dir),
            "fake_allowed_senders": "alice",
        }
        config.update(overrides)
        return config

    def make_daemon(self, config=None, session_factory=plain_factory):
        daemon = EdgeDaemon(
            config if config is not None else self.base_config(),
            clock=self.clock,
            session_factory=session_factory,
        )
        self.addCleanup(daemon.shutdown)
        daemon.start()
        return daemon

    # -- fake channel helpers --------------------------------------------------

    def send_inbound(self, text, sender="alice", thread="t1"):
        self.chan_dir.mkdir(parents=True, exist_ok=True)
        with open(self.chan_dir / "inbound.jsonl", "a") as handle:
            handle.write(json.dumps({
                "sender": sender, "text": text, "thread_id": thread,
            }) + "\n")

    def outbound(self):
        try:
            lines = (self.chan_dir / "outbound.jsonl").read_text()
        except OSError:
            return []
        return [json.loads(line) for line in lines.splitlines() if line]


class TestFakeChannel(IntakeCase):
    def test_send_poll_and_cursor(self):
        config = self.base_config()
        channel = FakeChannel(config)
        self.assertTrue(channel.is_configured())
        ok, _ = channel.send("hello out", thread_id="t9")
        self.assertTrue(ok)
        self.assertEqual(self.outbound()[0]["text"], "hello out")
        self.send_inbound("first")
        self.send_inbound("", sender="alice")  # empty text skipped
        with (self.chan_dir / "inbound.jsonl").open("a") as handle:
            handle.write("not json\n")
        state = {}
        messages = channel.poll(state)
        self.assertEqual([m.text for m in messages], ["first"])
        # the cursor advanced over everything, including junk lines
        self.assertEqual(channel.poll(state), [])
        self.send_inbound("second")
        self.assertEqual([m.text for m in channel.poll(state)], ["second"])

    def test_allowlist_fails_closed_via_manager(self):
        config = self.base_config()
        manager = ChannelManager(config)
        self.assertEqual(manager.configured(), ["fake"])
        self.send_inbound("from stranger", sender="mallory")
        self.send_inbound("from friend", sender="alice")
        with patch("sys.stderr"):
            inbound = manager.poll_all()
        self.assertEqual([m.sender for m in inbound], ["alice"])

    def test_no_allowlist_means_no_inbound(self):
        config = self.base_config()
        config.pop("fake_allowed_senders")
        manager = ChannelManager(config)
        self.send_inbound("anyone home?")
        with patch("sys.stderr"):
            self.assertEqual(manager.poll_all(), [])


class TestDaemonAnswersChannels(IntakeCase):
    def test_inbound_answered_with_no_shell_attached(self):
        daemon = self.make_daemon()
        self.send_inbound("how are you?")

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, **kwargs):
            return "answered headlessly", {"total_tokens": 2}

        with patch("conch.runtime.chat_turn", fake_chat_turn):
            stats = daemon.tick()
        self.assertEqual(stats.get("intake"), 1)
        replies = self.outbound()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["text"], "answered headlessly")
        self.assertEqual(replies[0]["thread_id"], "t1")
        self.assertIn("answered 1 inbound message",
                      daemon.log_path.read_text())
        # the daemon holds the intake lease it polled under
        lease = daemon.store.get_lease(
            INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
        )
        self.assertEqual(lease["holder"], daemon.holder)

    def test_non_allowlisted_sender_dropped_in_daemon(self):
        daemon = self.make_daemon()
        self.send_inbound("do bad things", sender="mallory")
        called = []
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: called.append(1) or ("x", {})), \
             patch("sys.stderr"):
            daemon.tick()
        self.assertEqual(called, [])
        self.assertEqual(self.outbound(), [])

    def test_remote_invariants_hold_in_daemon_turns(self):
        daemon = self.make_daemon()
        self.send_inbound("check something")
        seen = {}

        def recording_chat_turn(config, provider, raw_fn, messages, tools,
                                tool_map, builtin_clients, **kwargs):
            seen["system"] = messages[0]["content"]
            seen["tools"] = tools or []
            seen["clients"] = builtin_clients
            return "ok", {"total_tokens": 1}

        with patch("conch.runtime.chat_turn", recording_chat_turn):
            daemon.tick()
        self.assertIn("operating REMOTELY over fake", seen["system"])
        tool_names = {
            t.get("function", {}).get("name") for t in seen["tools"]
        }
        self.assertFalse(tool_names & REMOTE_EXCLUDED_TOOLS)
        self.assertFalse(set(seen["clients"]) & REMOTE_EXCLUDED_TOOLS)
        self.assertIsInstance(seen["clients"]["local_shell"],
                              RemoteShellClient)

    def test_reply_length_stays_bounded(self):
        daemon = self.make_daemon()
        self.send_inbound("tell me everything")
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("x" * 10_000, {})):
            daemon.tick()
        replies = self.outbound()
        self.assertEqual(len(replies), 1)
        self.assertLessEqual(len(replies[0]["text"]), 3100)

    def test_thread_maps_to_one_conversation(self):
        daemon = self.make_daemon()
        with patch("conch.runtime.chat_turn", lambda *a, **k: ("ok", {})):
            self.send_inbound("first", thread="t7")
            daemon.tick()
            self.clock.advance(2)
            self.send_inbound("second", thread="t7")
            daemon.tick()
        sessions = json.loads(
            (self.root / "state" / "conch" / "remote_sessions.json")
            .read_text()
        )
        self.assertEqual(list(sessions), ["fake:t7"])
        self.assertEqual(len(self.outbound()), 2)

    def test_status_reports_intake_lease(self):
        daemon = self.make_daemon()
        with patch("conch.runtime.chat_turn", lambda *a, **k: ("ok", {})):
            daemon.tick()
        status = control.request("status")
        self.assertEqual(status["channel_intake"]["holder"], daemon.holder)


class TestMissionAddressedInput(IntakeCase):
    def test_input_command_wakes_mission_same_tick(self):
        daemon = self.make_daemon(
            session_factory=input_requesting_factory()
        )
        mission_id = daemon.engine.create_mission({
            "goal": "needs input", "budgets": {}, "cadence_seconds": 3600,
        }, activate=True)
        with patch("conch.runtime.chat_turn", lambda *a, **k: ("ok", {})):
            daemon.tick()
        self.assertEqual(
            daemon.store.get_mission(mission_id)["status"],
            MissionState.WAITING_INPUT,
        )
        self.send_inbound(f"input {mission_id} use the conch repo")
        self.clock.advance(2)
        called = []
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: called.append(1) or ("x", {})):
            stats = daemon.tick()
        # the addressed input never runs a model turn ...
        self.assertEqual(called, [])
        self.assertEqual(stats.get("intake"), 1)
        # ... but the woken mission's session fired inside this same tick
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["fired"], 0)
        mission = daemon.store.get_mission(mission_id)
        self.assertEqual(mission["runs"], 2)
        replies = self.outbound()
        self.assertIn("wakes now", replies[-1]["text"])
        self.assertIn(mission_id, replies[-1]["text"])

    def test_input_command_for_unknown_mission_replies_error(self):
        daemon = self.make_daemon()
        self.send_inbound("input msn-000-000 hello there")
        called = []
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: called.append(1) or ("x", {})):
            daemon.tick()
        self.assertEqual(called, [])
        replies = self.outbound()
        self.assertIn("Mission input failed", replies[0]["text"])


class TestSingleConsumer(IntakeCase):
    """Exactly one reply per inbound message, both directions, with
    immediate handoff when the holder releases."""

    def shell_loop(self, config):
        gate = _ShellIntakeGate(config)
        state = ToolRuntimeState(all_tools=[], tool_map={}, tools=[])
        loop = RemoteLoop(
            config, conv_mgr=None, chat_state=state, builtin_clients={},
            intake_gate=gate, mission_input=gate.mission_input,
        )
        self.addCleanup(loop.stop)
        return loop, gate

    def test_daemon_first_then_handoff_to_shell(self):
        config = self.base_config(edge_daemon="true")
        daemon = self.make_daemon(config=config)
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("daemon reply", {})):
            daemon.tick()  # acquires the intake lease
        loop, gate = self.shell_loop(config)
        self.send_inbound("who answers?")
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("shell reply", {})):
            self.assertEqual(loop.poll_once(), 0,
                             "shell must not poll while the daemon holds"
                             " the intake lease")
        self.clock.advance(2)
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("daemon reply", {})):
            daemon.tick()
        replies = self.outbound()
        self.assertEqual([r["text"] for r in replies], ["daemon reply"])
        # graceful daemon shutdown releases the lease -> shell takes over
        daemon.shutdown()
        self.send_inbound("and now?")
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("shell reply", {})):
            self.assertEqual(loop.poll_once(), 1)
        replies = self.outbound()
        self.assertEqual(
            [r["text"] for r in replies], ["daemon reply", "shell reply"],
            "exactly one reply per message across the handoff",
        )

    def test_shell_first_then_handoff_to_daemon(self):
        config = self.base_config(edge_daemon="true")
        daemon = self.make_daemon(config=config)
        loop, gate = self.shell_loop(config)
        self.assertTrue(gate.acquire(), "shell wins the empty lease")
        self.send_inbound("who answers?")
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("daemon reply", {})):
            stats = daemon.tick()
        self.assertNotIn("intake", stats)
        self.assertIn("lease held by 'shell-", daemon.log_path.read_text())
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("shell reply", {})):
            self.assertEqual(loop.poll_once(), 1)
        self.assertEqual(
            [r["text"] for r in self.outbound()], ["shell reply"]
        )
        # stopping the shell loop releases the lease -> daemon takes over
        loop.stop()
        self.send_inbound("and now?")
        self.clock.advance(2)
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("daemon reply", {})):
            daemon.tick()
        self.assertEqual(
            [r["text"] for r in self.outbound()],
            ["shell reply", "daemon reply"],
        )

    def test_remote_host_shell_daemon_abstains_and_nothing_drops(self):
        config = self.base_config(
            edge_daemon="true", remote_host="shell",
        )
        daemon = self.make_daemon(config=config)
        self.send_inbound("waiting for the right host")
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("daemon reply", {})):
            stats = daemon.tick()
        self.assertNotIn("intake", stats)
        self.assertIsNone(daemon.store.get_lease(
            INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
        ), "remote_host=shell: the daemon never takes the intake lease")
        self.assertEqual(self.outbound(), [])
        # the message was not consumed: the shell picks it up unharmed
        loop, gate = self.shell_loop(config)
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("shell reply", {})):
            self.assertEqual(loop.poll_once(), 1)
        self.assertEqual(
            [r["text"] for r in self.outbound()], ["shell reply"]
        )

    def test_superseded_daemon_stops_polling_immediately(self):
        config = self.base_config(edge_daemon="true")
        zombie = self.make_daemon(config=config)
        with patch("conch.runtime.chat_turn", lambda *a, **k: ("z", {})):
            zombie.tick()  # zombie holds the intake lease
        # the zombie loses the kernel lock without releasing resources
        zombie._lock.release()
        successor = EdgeDaemon(
            config, clock=self.clock, session_factory=plain_factory,
            socket_path=self.root / "run2" / "edge.sock",
        )
        self.addCleanup(successor.shutdown)
        successor.start()
        self.send_inbound("who is alive?")
        self.clock.advance(2)
        # The zombie's whole tick is epoch-fenced at its first kernel write
        # (run_forever catches and logs this in production) ...
        from conch.kernel.model import KernelError

        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("zombie reply", {})):
            with self.assertRaises(KernelError):
                zombie.tick()
        # ... and the intake pass specifically refuses its lease and never
        # polls the channels.
        self.assertEqual(zombie.intake.tick(), 0)
        self.assertIn("lease refused", zombie.log_path.read_text())
        self.assertEqual(self.outbound(), [], "a fenced zombie never polls")
        # after the zombie's lease expires the successor answers
        self.clock.advance(intake_lease_seconds(1) + 1)
        with patch("conch.runtime.chat_turn",
                   lambda *a, **k: ("successor reply", {})):
            stats = successor.tick()
        self.assertEqual(stats.get("intake"), 1)
        self.assertEqual(
            [r["text"] for r in self.outbound()], ["successor reply"]
        )

    def test_lease_renews_across_passes(self):
        daemon = self.make_daemon()
        with patch("conch.runtime.chat_turn", lambda *a, **k: ("ok", {})):
            daemon.tick()
            first = daemon.store.get_lease(
                INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
            )
            self.clock.advance(2)
            daemon.tick()
            second = daemon.store.get_lease(
                INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
            )
        self.assertEqual(first["holder"], daemon.holder)
        self.assertEqual(second["holder"], daemon.holder)
        self.assertGreater(second["expires_at"], first["expires_at"])


class TestBootstrapHostSelection(IntakeCase):
    def test_daemon_hosted_is_the_kernel_mode_default(self):
        loop, reason = start_remote_loop(self.base_config(
            edge_daemon="true",
        ))
        self.assertIsNone(loop)
        self.assertEqual(reason, "daemon-hosted")

    def test_remote_host_shell_starts_gated_loop(self):
        loop, reason = start_remote_loop(self.base_config(
            edge_daemon="true", remote_host="shell",
        ))
        self.assertIsNotNone(loop)
        self.addCleanup(loop.stop)
        self.assertEqual(reason, "")
        self.assertIsNotNone(loop._intake_gate)
        self.assertIsNotNone(loop._mission_input)

    def test_no_kernel_mode_keeps_classic_ungated_loop(self):
        loop, reason = start_remote_loop(self.base_config())
        self.assertIsNotNone(loop)
        self.addCleanup(loop.stop)
        self.assertEqual(reason, "")
        self.assertIsNone(loop._intake_gate)
        self.assertIsNone(loop._mission_input)

    def test_shell_only_remote_loop_never_imports_kernel(self):
        """The no-daemon invariant extends to channel intake: with
        edge_daemon unset, hosting the remote loop must not load
        conch.kernel."""
        import subprocess
        import sys

        code = (
            "import json, sys, tempfile\n"
            "from conch.bootstrap import start_remote_loop\n"
            "chan = tempfile.mkdtemp()\n"
            "loop, reason = start_remote_loop({\n"
            "    'remote_enabled': 'true', 'fake_channel_dir': chan,\n"
            "    'fake_allowed_senders': 'alice',\n"
            "})\n"
            "loop.stop()\n"
            "print(json.dumps({\n"
            "    'reason': reason,\n"
            "    'kernel_imported': any(\n"
            "        name.startswith('conch.kernel')\n"
            "        for name in sys.modules\n"
            "    ),\n"
            "}))\n"
        )
        env = dict(os.environ)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=60, env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        probe = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(probe["reason"], "")
        self.assertFalse(probe["kernel_imported"])


class TestIntakeConfig(IntakeCase):
    def test_disabled_without_remote_enabled(self):
        config = self.base_config()
        config.pop("remote_enabled")
        daemon = self.make_daemon(config=config)
        self.send_inbound("anyone?")
        daemon.tick()
        self.assertEqual(self.outbound(), [])
        self.assertIsNone(daemon.store.get_lease(
            INTAKE_LEASE_KIND, INTAKE_LEASE_RESOURCE
        ))

    def test_no_channel_configured_logs_once(self):
        config = self.base_config()
        config.pop("fake_channel_dir")
        daemon = self.make_daemon(config=config)
        daemon.tick()
        self.clock.advance(2)
        daemon.tick()
        log = daemon.log_path.read_text()
        self.assertEqual(log.count("no channel is configured"), 1)

    def test_lease_ttl_scales_with_poll_interval(self):
        self.assertEqual(intake_lease_seconds(60), 180.0)
        self.assertEqual(intake_lease_seconds(1), 90.0)
        self.assertEqual(intake_lease_seconds("junk"), 180.0)


if __name__ == "__main__":
    unittest.main()
