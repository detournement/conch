"""The capitol_control session tool: the model-callable Capitol runtime
surface, against the scripted fake A2A gateway.

Proven here: the op→adapter mapping for every runtime read; keyed starts
with the derived-key contract (omitting the key digests workflow+inputs,
so an accidental retry replays instead of double-starting); the bounded
watch-and-summarize loop including its hard-deadline behavior; both HITL
respond kinds with the literal intervention token protocol; quarantine-
bounded artifact upload/download; admin/provisioning and pack ops refused
with the user-explicit /capitol command named; remote/channel gating
(reads and HITL pass, effectful start pins an origin-bound approval whose
consume constructs the exact frozen request); the personal_items
availability precedent (excluded from delegated sub-turns and fleet
envelopes unless explicitly named); and the mission-session swap (the
envelope-scoped capitol_control replaces this one).
"""

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conch.capitol.tool import (
    ADMIN_REFUSALS,
    CAPITOL_SESSION_TOOL,
    CapitolSessionClient,
    REMOTE_START_KIND,
    consume_capitol_start,
    derive_idempotency_key,
)
from conch.policy import (
    register_required_policy,
    unregister_required_policy,
)
from conch.remote import REMOTE_EXCLUDED_TOOLS, ApprovalStore, RemoteLoop
from conch.tooling import (
    DelegateTaskClient,
    ToolRuntimeState,
    inject_builtin_tools,
)

from tests.test_capitol_client import AGENT, BEARER, ORG, FakeGateway


def _event(sequence, event_type="node.node_started", **extra):
    event = {
        "run_id": "r", "sequence": sequence, "event_type": event_type,
        "scope": "node",
        "node": {"node_id": f"n{sequence}", "node_type": "agent",
                 "display_name": f"Node {sequence}"},
        "data": {},
    }
    event.update(extra)
    return event


class ToolCase(unittest.TestCase):
    """Fake gateway + isolated XDG dirs + bearer from the environment."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeGateway.reset(self.port)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "CAPITOL_A2A_BEARER": BEARER,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {
            "capitol_base_url": f"http://127.0.0.1:{self.port}",
            "capitol_org": ORG,
            "capitol_agent": AGENT,
        }
        self.client = CapitolSessionClient(self.config)

    def call(self, args):
        result = self.client.call_tool("capitol_control", args)
        return result["content"][0]["text"]

    def quarantine(self) -> Path:
        from conch.channels import quarantine_dir

        directory = quarantine_dir()
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class TestToolDefinition(ToolCase):
    def test_shape(self):
        fn = CAPITOL_SESSION_TOOL["function"]
        self.assertEqual(fn["name"], "capitol_control")
        self.assertEqual(fn["parameters"]["required"], ["op"])
        ops = set(fn["parameters"]["properties"]["op"]["enum"])
        self.assertEqual(ops, {
            "discover", "workflows", "describe", "suggest", "versions",
            "stats", "runs", "start", "status", "watch", "respond",
            "outputs", "evals", "upload", "download",
        })
        # Admin ops are never advertised on the schema.
        self.assertFalse(ops & set(ADMIN_REFUSALS))

    def test_registered_when_capitol_configured(self):
        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore

        clients = make_builtin_clients(
            MemoryStore(), dict(self.config, provider="openai")
        )
        self.assertIsInstance(
            clients["capitol_control"], CapitolSessionClient
        )
        tools: list = []
        tool_map: dict = {}
        with patch("conch.tooling.discover_user_tools",
                   return_value=([], object())):
            inject_builtin_tools(tools, tool_map, clients)
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("capitol_control", names)
        self.assertIs(tool_map["capitol_control"],
                      clients["capitol_control"])

    def test_absent_without_capitol_config(self):
        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore

        clients = make_builtin_clients(
            MemoryStore(), {"provider": "openai"}
        )
        self.assertNotIn("capitol_control", clients)


class TestReads(ToolCase):
    def test_discover_card_and_org_agents(self):
        text = self.call({"op": "discover"})
        self.assertIn("Fake eBay Sales Operator", text)
        self.assertIn("wire schema: 1.0.11", text)
        self.assertIn("call_workflow", text)
        self.assertIn("org agents (1):", text)
        self.assertIn(AGENT, text)

    def test_workflows_lists_ids_verbatim(self):
        text = self.call({"op": "workflows"})
        self.assertIn("draft-wf", text)
        self.assertIn("publish-wf", text)
        self.assertIn("Draft or Revise Listing", text)

    def test_describe_names_the_inputs_key(self):
        text = self.call({"op": "describe", "workflow_id": "draft-wf"})
        self.assertIn("'node-json-input.value'", text)
        self.assertIn("node_instance_id", text)

    def test_suggest_asks_for_confirmation(self):
        text = self.call({"op": "suggest", "goal": "sell my old lamp"})
        self.assertIn("draft-wf", text)
        self.assertIn("confidence=0.9", text)
        self.assertIn("confirm the workflow with the user", text)

    def test_versions_stats_runs_status_outputs(self):
        FakeGateway.runs["run-9"] = {
            "status": "success", "output": {"result": {"answer": 42}},
        }
        self.assertIn('"version_id": "v2"', self.call(
            {"op": "versions", "workflow_id": "draft-wf"}
        ))
        self.assertIn('"success_rate": 0.75', self.call(
            {"op": "stats", "workflow_id": "draft-wf", "days": 7}
        ))
        self.assertIn("run-9", self.call(
            {"op": "runs", "workflow_id": "draft-wf"}
        ))
        self.assertIn('"status": "success"', self.call(
            {"op": "status", "run_id": "run-9"}
        ))
        self.assertIn('"answer": 42', self.call(
            {"op": "outputs", "run_id": "run-9"}
        ))

    def test_evals_uniform_shape_without_nodes(self):
        FakeGateway.runs["run-3"] = {"status": "success", "output": {}}
        text = self.call({"op": "evals", "run_id": "run-3"})
        self.assertIn("has_evals=False", text)
        self.assertIn("suite_passed=False", text)

    def test_missing_argument_is_reported(self):
        self.assertIn("needs workflow_id",
                      self.call({"op": "describe"}))

    def test_auth_error_parks_with_sources(self):
        with patch.dict(os.environ, {"CAPITOL_A2A_BEARER": "cap_a2a_WRONG"}):
            text = self.call({"op": "workflows"})
        self.assertIn("credential needed", text)
        self.assertIn("CAPITOL_A2A_BEARER", text)
        self.assertIn("No automatic re-auth", text)

    def test_capability_gap_fails_closed_with_name(self):
        FakeGateway.card = dict(
            FakeGateway.card,
            skills=[{"id": "handshake"}, {"id": "list_workflows"}],
        )
        text = self.call({"op": "suggest", "goal": "anything"})
        self.assertIn("does not advertise", text)
        self.assertIn("suggest_workflow", text)


class TestStart(ToolCase):
    def test_keyed_start(self):
        text = self.call({
            "op": "start", "workflow_id": "draft-wf",
            "inputs": {"node-json-input.value": {"x": 1}},
            "idempotency_key": "my-key-1",
        })
        self.assertIn("run run-1 started", text)
        self.assertIn("my-key-1", text)
        skill, data, _ = FakeGateway.calls[-1]
        self.assertEqual(skill, "call_workflow")
        self.assertEqual(data["idempotency_key"], "my-key-1")
        self.assertEqual(data["inputs"], {"node-json-input.value": {"x": 1}})

    def test_omitted_key_is_derived_and_replays(self):
        args = {
            "op": "start", "workflow_id": "draft-wf",
            "inputs": {"node-json-input.value": {"x": 1}},
        }
        first = self.call(args)
        expected = derive_idempotency_key(
            "draft-wf", {"node-json-input.value": {"x": 1}}
        )
        self.assertIn(expected, first)
        self.assertIn("derived from workflow+inputs", first)
        second = self.call(args)
        self.assertIn("replayed", second)
        # One run on the gateway, not two.
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(FakeGateway.idempotency), 1)

    def test_input_value_wraps_under_discovered_key(self):
        self.call({
            "op": "start", "workflow_id": "draft-wf",
            "input_value": "after:2026/09/01 before:2026/09/08",
        })
        _, data, _ = FakeGateway.calls[-1]
        self.assertEqual(
            data["inputs"],
            {"node-json-input.value": "after:2026/09/01 before:2026/09/08"},
        )

    def test_inputs_and_input_value_conflict(self):
        text = self.call({
            "op": "start", "workflow_id": "draft-wf",
            "inputs": {"a": 1}, "input_value": "b",
        })
        self.assertIn("not both", text)

    def test_required_policy_denies_start(self):
        register_required_policy(
            "test-deny-start",
            lambda event, payload: event != "capitol.run.start",
        )
        self.addCleanup(unregister_required_policy, "test-deny-start")
        text = self.call({
            "op": "start", "workflow_id": "draft-wf", "inputs": {},
        })
        self.assertIn("denied by required policy", text)
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(starts, [])

    def test_artifacts_ride_the_start(self):
        self.call({
            "op": "start", "workflow_id": "draft-wf",
            "inputs": {}, "artifacts": ["art-7"],
        })
        _, data, _ = FakeGateway.calls[-1]
        self.assertEqual(data["artifacts"], [{"artifact_id": "art-7"}])


class TestWatch(ToolCase):
    def test_summarizes_to_terminal(self):
        FakeGateway.runs["run-1"] = {"status": "success", "output": {}}
        FakeGateway.run_events["run-1"] = [
            _event(1), _event(2, "node.tool_call"),
            _event(3, "workflow.run_completed"),
        ]
        text = self.call({"op": "watch", "run_id": "run-1"})
        self.assertIn("terminal state success", text)
        self.assertIn("3 event(s) observed, last sequence 3", text)
        self.assertIn("node.node_started×1", text)
        self.assertIn("node.tool_call×1", text)
        self.assertIn("op='outputs'", text)

    def test_relays_hitl_verbatim(self):
        FakeGateway.runs["run-1"] = {"status": "success", "output": {}}
        FakeGateway.run_events["run-1"] = [
            _event(1, "node.input_required", data={
                "request_id": "req-77", "input_kind": "clarification",
                "prompt": "What color is the lamp?",
            }),
            _event(2, "workflow.run_completed"),
        ]
        text = self.call({"op": "watch", "run_id": "run-1"})
        self.assertIn("NEEDS INPUT [clarification] request_id=req-77",
                      text)
        self.assertIn("What color is the lamp?", text)
        self.assertIn("relay the question to the user verbatim", text)

    def test_deadline_bounds_the_loop(self):
        FakeGateway.runs["run-1"] = {"status": "running", "output": {}}
        FakeGateway.run_events["run-1"] = [_event(1), _event(2)]
        clock = {"now": 0.0}

        def fake_clock():
            return clock["now"]

        def fake_sleep(seconds):
            clock["now"] += max(seconds, 1.0)

        result = self.client._op_watch(
            {"run_id": "run-1", "deadline_seconds": 10},
            _sleep=fake_sleep, _clock=fake_clock,
        )
        text = result["content"][0]["text"]
        self.assertIn("still running at the 10s watch deadline", text)
        self.assertIn("since_sequence=3", text)
        self.assertLessEqual(clock["now"], 12.0)

    def test_deadline_is_clamped(self):
        FakeGateway.runs["run-1"] = {"status": "success", "output": {}}
        FakeGateway.run_events["run-1"] = []
        text = self.call({
            "op": "watch", "run_id": "run-1",
            "deadline_seconds": 86400,
        })
        # Terminal immediately; the clamp shows in no hang and the text.
        self.assertIn("terminal state success", text)

    def test_resume_cursor_skips_seen_events(self):
        FakeGateway.runs["run-1"] = {"status": "success", "output": {}}
        FakeGateway.run_events["run-1"] = [
            _event(1), _event(2), _event(3),
        ]
        text = self.call({
            "op": "watch", "run_id": "run-1", "since_sequence": 3,
        })
        self.assertIn("1 event(s) observed, last sequence 3", text)


class TestRespond(ToolCase):
    def test_clarification_answer(self):
        text = self.call({
            "op": "respond", "run_id": "run-1", "request_id": "req-9",
            "response": "It is red.",
        })
        self.assertIn("clarification answered", text)
        skill, data, _ = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_clarification_response")
        self.assertEqual(data["request_id"], "req-9")
        self.assertEqual(data["response"], "It is red.")
        self.assertIs(data["declined"], False)

    def test_clarification_decline(self):
        self.call({
            "op": "respond", "run_id": "run-1", "request_id": "req-9",
            "decline": True,
        })
        _, data, _ = FakeGateway.calls[-1]
        self.assertIs(data["declined"], True)

    def test_intervention_token_protocol(self):
        text = self.call({
            "op": "respond", "run_id": "run-1", "request_id": "req-9",
            "kind": "intervention", "response": "yes go ahead",
        })
        self.assertIn("literal tokens 'continue' or 'stop'", text)
        self.assertNotIn("submit_intervention_response",
                         [c[0] for c in FakeGateway.calls])
        text = self.call({
            "op": "respond", "run_id": "run-1", "request_id": "req-9",
            "kind": "intervention", "response": "continue",
            "node_id": "n1",
        })
        self.assertIn("intervention 'continue' delivered", text)
        skill, data, _ = FakeGateway.calls[-1]
        self.assertEqual(skill, "submit_intervention_response")
        self.assertEqual(data["node_id"], "n1")
        self.assertEqual(data["response"], "continue")

    def test_required_policy_denies_respond(self):
        register_required_policy(
            "test-deny-respond",
            lambda event, payload: event != "capitol.hitl.respond",
        )
        self.addCleanup(unregister_required_policy, "test-deny-respond")
        text = self.call({
            "op": "respond", "run_id": "run-1", "request_id": "req-9",
            "response": "x",
        })
        self.assertIn("denied by required policy", text)


class TestAdminAndPackRefusals(ToolCase):
    def test_admin_ops_refused_with_command(self):
        for op, command in (
            ("publish", "/capitol admin publish <workflow>"),
            ("create-agent",
             "/capitol admin create-agent <name> --workflows a,b"),
            ("persist", "/capitol admin persist @payload.json"),
            ("rollback", "/capitol admin rollback <workflow>"),
            ("allowlist", "/capitol admin allowlist <agent> <wf,…>"),
            ("schedule-add", "/capitol admin schedule-add <wf> <name> <cron>"),
        ):
            text = self.call({"op": op})
            self.assertIn("refuses", text)
            self.assertIn(command, text)
            self.assertIn("user-explicit", text)
        # Refusals never touch the wire.
        self.assertEqual(FakeGateway.calls, [])

    def test_pack_mutations_refused(self):
        text = self.call({"op": "pack"})
        self.assertIn("/capitol pack list|show|verify", text)
        self.assertIn("user-edited", text)
        self.assertEqual(FakeGateway.calls, [])

    def test_lifecycle_ops_point_elsewhere(self):
        self.assertIn("/capitol stop <run>", self.call({"op": "stop"}))
        self.assertIn("/capitol chat", self.call({"op": "chat"}))
        self.assertEqual(FakeGateway.calls, [])

    def test_unknown_op(self):
        self.assertIn("Unknown capitol_control op",
                      self.call({"op": "frobnicate"}))


class TestArtifacts(ToolCase):
    def test_upload_requires_quarantine_path(self):
        outside = self.root / "elsewhere.bin"
        outside.write_bytes(b"data")
        text = self.call({"op": "upload", "path": str(outside)})
        self.assertIn("quarantine dir", text)
        self.assertEqual(FakeGateway.uploads, {})

    def test_upload_from_quarantine(self):
        path = self.quarantine() / "photo.jpg"
        path.write_bytes(b"\xff\xd8\xff fake jpeg")
        text = self.call({"op": "upload", "path": str(path)})
        self.assertIn("artifact_id art-1", text)
        self.assertIn("digest sha256:", text)
        self.assertEqual(
            FakeGateway.uploads["art-1"]["bytes"], b"\xff\xd8\xff fake jpeg"
        )

    def test_download_lands_in_quarantine_sanitized(self):
        FakeGateway.blobs["file-9"] = b"PK\x03\x04 docx bytes"
        text = self.call({
            "op": "download", "file_id": "file-9",
            "filename": "../../evil/packet.docx",
        })
        self.assertIn("downloaded to", text)
        self.assertIn("verify-on-fetch", text)
        quarantine = self.quarantine()
        files = list(quarantine.glob("capitol-*"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"PK\x03\x04 docx bytes")
        self.assertNotIn("..", files[0].name)
        self.assertTrue(str(files[0]).startswith(str(quarantine)))


class TestRemoteGating(ToolCase):
    """Channel sessions: reads and HITL pass; start pins an approval."""

    def _remote_client(self, notes):
        return CapitolSessionClient(
            self.config,
            remote_origin={"channel": "slack", "thread_id": "42.1",
                           "sender": "U111"},
            approvals=ApprovalStore(),
            notify=lambda text, tid: notes.append((text, tid)),
        )

    def test_reads_and_respond_pass(self):
        notes = []
        client = self._remote_client(notes)
        text = client.call_tool(
            "capitol_control", {"op": "workflows"}
        )["content"][0]["text"]
        self.assertIn("draft-wf", text)
        text = client.call_tool("capitol_control", {
            "op": "respond", "run_id": "run-1", "request_id": "req-1",
            "response": "blue",
        })["content"][0]["text"]
        self.assertIn("clarification answered", text)
        self.assertEqual(notes, [])

    def test_start_proposes_origin_bound_approval(self):
        notes = []
        client = self._remote_client(notes)
        text = client.call_tool("capitol_control", {
            "op": "start", "workflow_id": "draft-wf",
            "inputs": {"node-json-input.value": {"x": 1}},
        })["content"][0]["text"]
        self.assertIn("requires user approval", text)
        self.assertIn("Do not retry", text)
        # Nothing hit the wire.
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(starts, [])
        # The approval pins the exact payload to the origin.
        pending = ApprovalStore().pending()
        self.assertEqual(len(pending), 1)
        entry = list(pending.values())[0]
        self.assertEqual(entry["kind"], REMOTE_START_KIND)
        self.assertEqual(entry["channel"], "slack")
        self.assertEqual(entry["thread_id"], "42.1")
        self.assertEqual(entry["sender"], "U111")
        self.assertEqual(entry["payload"]["workflow_id"], "draft-wf")
        self.assertEqual(
            entry["payload"]["idempotency_key"],
            derive_idempotency_key(
                "draft-wf", {"node-json-input.value": {"x": 1}}
            ),
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("approve", notes[0][0])

    def _loop(self):
        state = ToolRuntimeState(all_tools=[], tool_map={}, tools=[])
        loop = RemoteLoop(
            dict(self.config, provider="openai"),
            conv_mgr=None, chat_state=state, builtin_clients={},
        )
        loop.manager.notify = lambda *a, **k: (True, "")
        return loop

    def _approve(self, loop, verb, request_id, sender="U111",
                 thread_id="42.1"):
        from conch.channels import InboundMessage

        with patch("sys.stderr", io.StringIO()):
            return loop.handle_inbound(InboundMessage(
                channel="slack", sender=sender,
                text=f"{verb} {request_id}", thread_id=thread_id,
            ))

    def _propose(self):
        notes = []
        client = self._remote_client(notes)
        client.call_tool("capitol_control", {
            "op": "start", "workflow_id": "draft-wf",
            "inputs": {"node-json-input.value": {"x": 1}},
        })
        return int(list(ApprovalStore().pending())[0])

    def test_approve_executes_the_pinned_start(self):
        request_id = self._propose()
        reply = self._approve(self._loop(), "approve", request_id)
        self.assertIn("Started", reply)
        self.assertIn("run run-1", reply)
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(
            starts[0][1]["inputs"], {"node-json-input.value": {"x": 1}}
        )
        self.assertEqual(ApprovalStore().pending(), {})

    def test_deny_starts_nothing(self):
        request_id = self._propose()
        reply = self._approve(self._loop(), "deny", request_id)
        self.assertIn("will not start", reply)
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(starts, [])

    def test_origin_mismatch_refused(self):
        request_id = self._propose()
        reply = self._approve(
            self._loop(), "approve", request_id, sender="U999"
        )
        self.assertIn("different sender", reply)
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(starts, [])

    def test_consume_rechecks_required_policy(self):
        request_id = self._propose()
        entry, _ = ApprovalStore().consume(
            request_id, channel="slack", thread_id="42.1", sender="U111",
        )
        register_required_policy(
            "test-deny-consume",
            lambda event, payload: event != "capitol.run.start",
        )
        self.addCleanup(unregister_required_policy, "test-deny-consume")
        reply = consume_capitol_start(
            request_id, entry, "approve", self.config
        )
        self.assertIn("denied by required policy", reply)
        starts = [c for c in FakeGateway.calls if c[0] == "call_workflow"]
        self.assertEqual(starts, [])

    def test_not_remote_excluded(self):
        self.assertNotIn("capitol_control", REMOTE_EXCLUDED_TOOLS)

    def test_remote_loop_swaps_in_origin_bound_client(self):
        loop = self._loop()
        clients = loop._remote_clients(
            {"capitol_control": self.client, "local_shell": object()},
            "slack", "42.1", "U111",
        )
        swapped = clients["capitol_control"]
        self.assertIsNot(swapped, self.client)
        self.assertEqual(swapped._remote["thread_id"], "42.1")


class TestSubTurnAndFleet(ToolCase):
    """The personal_items precedent: never implicit in delegated
    sub-turns or fleet workers; a skill's tool list or a task envelope
    naming it is the explicit offer."""

    def _delegate(self, tools):
        client = DelegateTaskClient()
        state = ToolRuntimeState(all_tools=tools, tool_map={}, tools=tools)
        builtins = {
            "local_shell": object(),
            "capitol_control": self.client,
            "delegate_task": client,
        }
        client.bind(dict(self.config, provider="openai"), state, builtins)
        return client

    def test_default_subturn_excludes_capitol_control(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, **kwargs):
            seen["tools"] = {t["function"]["name"] for t in tools}
            seen["clients"] = builtin_clients
            return "done", {}

        client = self._delegate([
            {"function": {"name": "local_shell"}},
            {"function": {"name": "capitol_control"}},
        ])
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "explore"})
        self.assertEqual(seen["tools"], {"local_shell"})
        self.assertNotIn("capitol_control", seen["clients"])

    def test_skill_scoped_subturn_offers_it_explicitly(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, **kwargs):
            seen["tools"] = {t["function"]["name"] for t in tools}
            seen["clients"] = builtin_clients
            return "done", {}

        client = self._delegate([
            {"function": {"name": "local_shell"}},
            {"function": {"name": "capitol_control"}},
        ])
        skill = {
            "name": "capitol", "description": "drive Capitol workflows",
            "body": "Discover, start keyed, watch bounded, report.",
            "tools": ["capitol_control"], "model": "", "provider": "",
            "rounds": 0,
        }
        with patch("conch.skills.get_skill", return_value=skill), \
             patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool(
                "delegate_task",
                {"task": "run the ingest backfill", "skill": "capitol"},
            )
        self.assertEqual(seen["tools"], {"capitol_control"})
        self.assertIn("capitol_control", seen["clients"])

    def _executor(self, tools):
        import time

        from conch.fleet.taskexec import TaskExecutor
        from conch.swarm.protocol import TaskEnvelope, new_id

        home = self.root / "worker-home"
        task_id = new_id("task")
        workspace = home / "workspaces" / task_id
        workspace.mkdir(parents=True)
        envelope = TaskEnvelope(
            task_id=task_id, mission_id=new_id("msn"),
            principal="test", task="do the thing",
            idempotency_key="idem-1", issued_at=time.time(),
            tools=tuple(tools),
        )
        (workspace / "task.json").write_text(json.dumps({
            "envelope": envelope.to_dict(),
        }))
        return TaskExecutor(home, task_id, 1)

    def test_not_in_worker_denylist(self):
        from conch.fleet.taskexec import WORKER_TOOL_DENYLIST

        self.assertNotIn("capitol_control", WORKER_TOOL_DENYLIST)

    def test_fleet_absent_unless_envelope_offers(self):
        with patch("conch.tooling.discover_user_tools",
                   return_value=([], object())):
            executor = self._executor(["local_shell"])
            clients, tools = executor._build_tools(
                dict(self.config, provider="openai")
            )
        self.assertNotIn("capitol_control", clients)
        self.assertNotIn(
            "capitol_control",
            {t["function"]["name"] for t in tools},
        )

    def test_fleet_present_when_envelope_offers(self):
        with patch("conch.tooling.discover_user_tools",
                   return_value=([], object())):
            executor = self._executor(["capitol_control"])
            clients, tools = executor._build_tools(
                dict(self.config, provider="openai")
            )
        self.assertIn("capitol_control", clients)
        self.assertIn(
            "capitol_control",
            {t["function"]["name"] for t in tools},
        )


class TestMissionSessionSwap(ToolCase):
    """Mission sessions keep the envelope-scoped capitol_control: the
    kernel engine drops the session tool before appending its own."""

    def test_factory_replaces_session_tool(self):
        from conch.kernel.engine import _default_session_factory

        session_def = {"function": {"name": "capitol_control"}}
        mission_client = object()

        class FakeState:
            tools = [
                {"function": {"name": "local_shell"}},
                session_def,
            ]

        class FakeSession:
            def __init__(self, *a, **k):
                self.budgets = None
                self.builtin_clients = {"capitol_control": object()}
                self.chat_state = FakeState()
                self.seen = {}

            def run_turn(self, messages, tools=None, **kwargs):
                self.seen["tools"] = tools
                self.seen["clients"] = dict(self.builtin_clients)
                return "ok", {}

            def close(self):
                pass

        fake_session = FakeSession()

        class Control:
            capitol = mission_client
            staged = {}

        with patch("conch.bootstrap.build_agent_session",
                   return_value=fake_session):
            run = _default_session_factory({"provider": "openai"})
            run(
                {"mission_id": "msn-1", "spec": {}},
                [{"role": "user", "content": "go"}],
                Control(),
                {"max_tool_rounds": 3, "token_budget": 1000},
            )
        tool_names = [
            t["function"]["name"] for t in fake_session.seen["tools"]
        ]
        self.assertEqual(tool_names.count("capitol_control"), 1)
        self.assertIs(
            fake_session.seen["clients"]["capitol_control"],
            mission_client,
        )

    def test_factory_drops_session_tool_without_envelope(self):
        from conch.kernel.engine import _default_session_factory

        class FakeState:
            tools = [{"function": {"name": "capitol_control"}}]

        class FakeSession:
            def __init__(self):
                self.budgets = None
                self.builtin_clients = {"capitol_control": object()}
                self.chat_state = FakeState()
                self.seen = {}

            def run_turn(self, messages, tools=None, **kwargs):
                self.seen["tools"] = tools
                self.seen["clients"] = dict(self.builtin_clients)
                return "ok", {}

            def close(self):
                pass

        fake_session = FakeSession()

        class Control:
            capitol = None
            staged = {}

        with patch("conch.bootstrap.build_agent_session",
                   return_value=fake_session):
            run = _default_session_factory({"provider": "openai"})
            run(
                {"mission_id": "msn-1", "spec": {}},
                [{"role": "user", "content": "go"}],
                Control(),
                {"max_tool_rounds": 3, "token_budget": 1000},
            )
        tool_names = [
            t["function"]["name"] for t in fake_session.seen["tools"]
        ]
        self.assertNotIn("capitol_control", tool_names)
        self.assertNotIn(
            "capitol_control", fake_session.seen["clients"]
        )


class TestDerivedKey(unittest.TestCase):
    def test_deterministic_and_input_sensitive(self):
        key_a = derive_idempotency_key("wf", {"a": 1})
        key_b = derive_idempotency_key("wf", {"a": 1})
        key_c = derive_idempotency_key("wf", {"a": 2})
        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertTrue(key_a.startswith("capitol-tool:wf:"))

    def test_canonical_ordering(self):
        self.assertEqual(
            derive_idempotency_key("wf", {"a": 1, "b": 2}),
            derive_idempotency_key("wf", {"b": 2, "a": 1}),
        )


if __name__ == "__main__":
    unittest.main()
