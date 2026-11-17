"""fleet_delegate gates: a local agent turn delegating to a real fleet
worker mid-conversation, with the authority-subset rule enforced in code.

The round-trip runs the REAL chat_turn: a scripted provider emits a
fleet_delegate tool call, the client direct-drives the fleet kernel
against a real worker supervisor subprocess over the fake SSH transport,
and the worker's summary returns as the tool result the model folds into
its final reply.
"""

import json
import tempfile
import unittest
from pathlib import Path

from conch.fleet import authority
from conch.fleet.client import DirectFleetClient
from conch.fleet.delegate import FLEET_DELEGATE_TOOL, FleetDelegateClient

from tests.fleet_fakes import FakeSSHWorkerTransport, LocalWorkerProcess


class DelegateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.kernel_dir = self.root / "fleet"
        self.kernel_dir.mkdir(parents=True)
        self._workers = {}

    def _factory(self, worker):
        return FakeSSHWorkerTransport(self._workers[worker["worker_id"]])

    def _attach(self, config):
        return DirectFleetClient(
            config, kernel_dir=self.kernel_dir,
            transport_factory=self._factory,
        )

    def start_worker(self, registry, name="w1", script_text="remote done"):
        home = self.root / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"agent_mode": True}))
        script = home / "script.json"
        script.write_text(json.dumps([{"content": script_text}]))
        worker_id = registry.enroll(
            name, host="10.0.0.9", trust_level=2,
            data_ceiling="confidential", max_concurrency=2,
        )
        registry.activate(worker_id)
        proc = LocalWorkerProcess(home, env_extra={
            "CONCH_FLEET_TASK_SCRIPT": str(script),
        }).start()
        self.addCleanup(proc.stop)
        self._workers[worker_id] = proc
        return worker_id

    def make_client(self, **kwargs):
        return FleetDelegateClient({}, attach=self._attach, **kwargs)


class TestDelegateRoundTrip(DelegateCase):
    def test_round_trip_inside_a_chat_turn(self):
        """The model calls fleet_delegate; the worker's summary comes back
        as the tool result; the turn completes with it in context."""
        from conch.runtime import chat_turn

        setup = self._attach({})
        self.start_worker(
            setup.registry, script_text="the remote host says 7"
        )
        setup.close()
        delegate = self.make_client()
        responses = [
            {"content": "", "tool_calls": [{
                "id": "fd1", "type": "function",
                "function": {"name": "fleet_delegate",
                             "arguments": json.dumps({
                                 "task": "compute the remote answer",
                                 "worker": "w1",
                             })},
            }]},
            {"content": "LOCAL FINAL: relayed the fleet result"},
        ]

        def raw_fn(config, messages, tools):  # noqa: ARG001
            index = sum(
                1 for message in messages
                if message.get("role") == "assistant"
            )
            response = dict(responses[min(index, len(responses) - 1)])
            response.setdefault("_usage", {
                "input_tokens": 1, "output_tokens": 1, "model": "scripted",
            })
            return response

        messages = [
            {"role": "system", "content": "test"},
            {"role": "user", "content": "delegate this"},
        ]
        reply, usage = chat_turn(
            {"provider": "openai"}, "openai", raw_fn, messages,
            [FLEET_DELEGATE_TOOL], {}, {"fleet_delegate": delegate},
            max_tool_rounds=3,
        )
        self.assertIn("LOCAL FINAL", reply)
        tool_messages = [
            message for message in messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("the remote host says 7", tool_messages[0]["content"])
        self.assertIn("succeeded", tool_messages[0]["content"])

    def test_worker_allowlist_enforced(self):
        setup = self._attach({})
        self.start_worker(setup.registry)
        setup.close()
        delegate = self.make_client(allowed_workers=["other"])
        result = delegate.call_tool("fleet_delegate", {
            "task": "x", "worker": "w1",
        })
        self.assertIn("not in this session's", result["content"][0]["text"])

    def test_caller_authority_clamps_the_child(self):
        """A delegate session without WRITE-class authority cannot push a
        wider envelope even to a granted worker."""
        setup = self._attach({})
        worker_id = self.start_worker(setup.registry)
        authority.apply_grant(
            setup.registry, worker_id,
            authority.validate_grant(["communicate"], None),
        )
        setup.close()
        delegate = self.make_client()  # DEFAULT_DELEGATE_AUTHORITY
        result = delegate.call_tool("fleet_delegate", {
            "task": "send mail", "worker": "w1",
            "tools": ["save_memory"],
        })
        text = result["content"][0]["text"]
        self.assertIn("refused", text.lower())
        self.assertIn("save_memory", text)

    def test_skill_allowlist_enforced(self):
        delegate = self.make_client(allowed_skills=["capitol"])
        result = delegate.call_tool("fleet_delegate", {
            "task": "x", "worker": "auto", "skill": "other",
        })
        self.assertIn("not in this session's", result["content"][0]["text"])

    def test_missing_task_is_an_error(self):
        delegate = self.make_client()
        result = delegate.call_tool("fleet_delegate", {})
        self.assertIn("required", result["content"][0]["text"])


class TestDelegateRegistration(unittest.TestCase):
    def test_bootstrap_registers_only_when_gated(self):
        from unittest.mock import patch

        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore

        with patch.object(MemoryStore, "__init__", return_value=None):
            memory = MemoryStore.__new__(MemoryStore)
        clients = make_builtin_clients(memory, {}, interactive=True)
        self.assertNotIn("fleet_delegate", clients)
        clients = make_builtin_clients(
            memory, {"fleet_controller": "true"}, interactive=True
        )
        self.assertIn("fleet_delegate", clients)

    def test_remote_sessions_exclude_fleet_delegate(self):
        from conch.remote import REMOTE_EXCLUDED_TOOLS

        self.assertIn("fleet_delegate", REMOTE_EXCLUDED_TOOLS)

    def test_workers_deny_fleet_delegate(self):
        from conch.fleet.authority import HARD_EXCLUDED_TOOLS
        from conch.fleet.taskexec import WORKER_TOOL_DENYLIST

        self.assertIn("fleet_delegate", WORKER_TOOL_DENYLIST)
        self.assertIn("fleet_delegate", HARD_EXCLUDED_TOOLS)

    def test_local_subagents_do_not_inherit_implicitly(self):
        from conch.tooling import DelegateTaskClient

        self.assertIn(
            "fleet_delegate",
            DelegateTaskClient.IMPLICITLY_EXCLUDED_TOOLS,
        )


if __name__ == "__main__":
    unittest.main()
