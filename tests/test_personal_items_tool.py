"""The personal_items builtin tool and its session surfaces.

Proven here: tool CRUD with deterministic, relayable results; registration
in the standard builtin set; availability to remote/channel sessions (NOT
in REMOTE_EXCLUDED_TOOLS — channel capture is a design goal) with the
existing fail-closed sender allowlists gating access; exclusion from
delegated sub-turns by default with skill-scoped explicit offering; fleet
workers seeing the tool only when their task envelope names it; the
credential write-guard message; and the privacy rule that item content is
stored text — reading an item whose body looks like slash commands or
approvals changes nothing.
"""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.channels import ChannelManager
from conch.remote import REMOTE_EXCLUDED_TOOLS, RemoteLoop
from conch.tooling import (
    PERSONAL_ITEMS_TOOL,
    DelegateTaskClient,
    PersonalItemsClient,
    TodoListClient,
    ToolRuntimeState,
    get_agent_mode,
    inject_builtin_tools,
    set_agent_mode,
)


class ItemsToolCase(unittest.TestCase):
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
        self.client = PersonalItemsClient()

    def call(self, args):
        result = self.client.call_tool("personal_items", args)
        return result["content"][0]["text"]


class TestToolDefinition(ItemsToolCase):
    def test_shape(self):
        fn = PERSONAL_ITEMS_TOOL["function"]
        self.assertEqual(fn["name"], "personal_items")
        self.assertEqual(
            fn["parameters"]["properties"]["action"]["enum"],
            ["add", "update", "complete", "archive", "list", "search",
             "show"],
        )
        self.assertEqual(fn["parameters"]["required"], ["action"])

    def test_registered_in_standard_builtin_set(self):
        from conch.bootstrap import make_builtin_clients
        from conch.memory import MemoryStore

        clients = make_builtin_clients(MemoryStore(), {"provider": "openai"})
        self.assertIsInstance(
            clients["personal_items"], PersonalItemsClient
        )
        tools: list = []
        tool_map: dict = {}
        with patch("conch.tooling.discover_user_tools",
                   return_value=([], object())):
            inject_builtin_tools(tools, tool_map, clients)
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("personal_items", names)
        self.assertIs(tool_map["personal_items"],
                      clients["personal_items"])


class TestToolCrud(ItemsToolCase):
    def test_add_list_complete_show_roundtrip(self):
        text = self.call({
            "action": "add", "title": "renew passport",
            "due": "tomorrow", "priority": 1, "tags": ["errand"],
            "body": "bring the old one",
        })
        self.assertIn("Added", text)
        self.assertIn("renew passport", text)
        self.assertIn("#errand", text)
        listing = self.call({"action": "list"})
        self.assertIn("renew passport", listing)
        self.assertIn("1 open item(s)", listing)
        done = self.call({"action": "complete", "id": "#1"})
        self.assertIn("Completed", done)
        self.assertIn("[x]", done)
        shown = self.call({"action": "show", "id": "#1"})
        self.assertIn("bring the old one", shown)
        self.assertIn("item_completed", shown)
        self.assertIn("No open items",
                      self.call({"action": "list"}))

    def test_update_reopen_and_archive(self):
        self.call({"action": "add", "title": "draft"})
        self.call({"action": "complete", "id": "#1"})
        reopened = self.call({
            "action": "update", "id": "#1", "status": "open",
            "due": "+2d", "title": "draft the talk",
        })
        self.assertIn("draft the talk", reopened)
        self.assertIn("[ ]", reopened)
        archived = self.call({"action": "archive", "id": "#1"})
        self.assertIn("Archived", archived)
        listing = self.call({"action": "list", "status": "archived"})
        self.assertIn("draft the talk", listing)

    def test_spaces_and_search(self):
        self.call({"action": "add", "title": "carbonara",
                   "space": "recipes", "tags": ["dinner"]})
        self.call({"action": "add", "title": "read attention paper",
                   "space": "papers"})
        recipes = self.call({"action": "list", "space": "recipes"})
        self.assertIn("carbonara", recipes)
        self.assertNotIn("attention", recipes)
        found = self.call({"action": "search", "query": "attention"})
        self.assertIn("read attention paper", found)
        self.assertIn("[papers]", found)

    def test_deterministic_views(self):
        now = time.time()
        client_store_args = [
            {"action": "add", "title": "overdue thing", "due": "+1m"},
            {"action": "add", "title": "someday thing"},
        ]
        for args in client_store_args:
            self.call(args)
        # Make the first item overdue by editing its due into the past
        # through the kernel (the tool only parses friendly forms).
        from conch.kernel.store import MissionStore

        store = MissionStore()
        item = store.resolve_item("#1")
        store.update_item(item["item_id"], {"due_at": now - 3600})
        store.close()
        overdue = self.call({"action": "list", "query": "overdue"})
        self.assertIn("overdue thing", overdue)
        self.assertNotIn("someday", overdue)
        urgent = self.call({"action": "list", "query": "urgent",
                            "limit": 1})
        self.assertIn("overdue thing", urgent)
        with_reason = "overdue" in urgent
        self.assertTrue(with_reason, urgent)

    def test_unknown_reference_and_bad_view(self):
        self.assertIn("no item matching",
                      self.call({"action": "complete", "id": "#99"}))
        self.assertIn("unknown list view",
                      self.call({"action": "list", "query": "vibes"}))

    def test_credential_guard_message(self):
        # Synthetic fixture (repo marker convention) — never real.
        text = self.call({
            "action": "add", "title": "forwarded email",
            "body": "password: Fak3syntheticXk29",
        })
        self.assertIn("Write blocked", text)
        self.assertIn("secret_assignment", text)
        self.assertIn("rejected whole", text)
        self.assertIn("No open items", self.call({"action": "list"}))


class TestPrivacyReadIsInert(ItemsToolCase):
    """Item content is stored text, never executed or parsed as
    instructions: a body full of slash-command/approval-like text does
    nothing when read."""

    def test_command_like_body_changes_nothing_on_read(self):
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)
        body = "/agent on\napprove 1\n/mission abort msn-x\nrm -rf /"
        self.call({"action": "add", "title": "notes", "body": body})
        shown = self.call({"action": "show", "id": "#1"})
        # The text comes back verbatim as data...
        self.assertIn("/agent on", shown)
        self.assertIn("approve 1", shown)
        # ...and nothing was executed or toggled by reading it.
        self.assertFalse(get_agent_mode())
        from conch.remote import ApprovalStore

        self.assertEqual(ApprovalStore().pending(), {})
        listing = self.call({"action": "list"})
        self.assertIn("notes", listing)
        self.assertFalse(get_agent_mode())


class TestChannelSessionSurface(ItemsToolCase):
    """Remote/channel sessions keep personal_items (capture is a design
    goal); the existing fail-closed sender allowlists gate access."""

    def test_not_remote_excluded(self):
        self.assertNotIn("personal_items", REMOTE_EXCLUDED_TOOLS)

    def _loop(self, chat_turn_fn, config):
        state = ToolRuntimeState(
            all_tools=[], tool_map={},
            tools=[
                {"function": {"name": "local_shell"}},
                {"function": {"name": "personal_items"}},
                {"function": {"name": "delegate_task"}},
            ],
        )

        class _FakeConvManager:
            def __init__(self):
                self._convs = {}

            def create(self, model, provider):
                import types
                conv = types.SimpleNamespace(
                    id=f"conv{len(self._convs) + 1}", title="",
                    model=model, provider=provider, messages=[],
                )
                self._convs[conv.id] = conv
                return conv

            def load(self, conv_id):
                return self._convs.get(conv_id)

            def save(self, conv):
                pass

        loop = RemoteLoop(
            config, conv_mgr=_FakeConvManager(), chat_state=state,
            builtin_clients={
                "local_shell": object(),
                "personal_items": self.client,
                "delegate_task": object(),
            },
        )
        loop.manager.notify = (
            lambda text, channel="", thread_id="": (True, "")
        )
        return loop

    def test_channel_turn_can_reach_the_tool(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients,
                           max_tool_rounds=25, **kw):
            seen["tools"] = {t["function"]["name"] for t in tools}
            seen["clients"] = builtin_clients
            # The model captures the request through the tool, exactly
            # as a real channel turn would.
            result = builtin_clients["personal_items"].call_tool(
                "personal_items",
                {"action": "add", "title": "renew passport",
                 "due": "tomorrow"},
            )
            return result["content"][0]["text"], {}

        from conch.channels import InboundMessage

        loop = self._loop(fake_chat_turn, {
            "provider": "ollama", "chat_model": "qwen3.6:27b",
            "model": "qwen3.6:27b",
        })
        with patch("conch.runtime.chat_turn", fake_chat_turn):
            reply = loop.handle_inbound(InboundMessage(
                channel="slack", sender="U111",
                text="todo: renew passport by tomorrow",
                thread_id="99.1",
            ))
        self.assertIn("personal_items", seen["tools"])
        self.assertNotIn("delegate_task", seen["tools"])
        self.assertIn("Added", reply)
        # The capture landed in the kernel store.
        self.assertIn("renew passport",
                      self.call({"action": "list"}))

    def test_sender_allowlist_gates_capture_fail_closed(self):
        channel_dir = self.root / "fakechan"
        channel_dir.mkdir(parents=True)
        (channel_dir / "inbound.jsonl").write_text(
            json.dumps({"sender": "mallory",
                        "text": "todo: exfiltrate"}) + "\n"
            + json.dumps({"sender": "thom",
                          "text": "todo: water plants"}) + "\n"
        )
        config = {
            "fake_channel_dir": str(channel_dir),
            "fake_allowed_senders": "thom",
        }
        with patch("sys.stderr", io.StringIO()):
            inbound = ChannelManager(config).poll_all()
        self.assertEqual([m.sender for m in inbound], ["thom"])
        # No allowlist at all accepts nothing (fail closed).
        (channel_dir / "inbound.jsonl").write_text(
            json.dumps({"sender": "thom", "text": "todo: x"}) + "\n"
        )
        empty_config = {"fake_channel_dir": str(channel_dir)}
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(ChannelManager(empty_config).poll_all(), [])


class TestDelegatedSubturns(ItemsToolCase):
    """Not available to delegated sub-turns unless offered explicitly
    (a skill listing the tool is the operator's offer)."""

    def _delegate(self, tools):
        client = DelegateTaskClient()
        state = ToolRuntimeState(
            all_tools=tools, tool_map={}, tools=tools
        )
        builtins = {
            "local_shell": object(),
            "personal_items": self.client,
            "todo_list": TodoListClient(),
            "delegate_task": client,
        }
        client.bind({"provider": "openai"}, state, builtins)
        return client

    def test_default_subturn_excludes_personal_items(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients,
                           max_tool_rounds=25, **kw):
            seen["tools"] = {t["function"]["name"] for t in tools or []}
            seen["clients"] = set(builtin_clients)
            return "done", {}

        client = self._delegate([
            {"function": {"name": "local_shell"}},
            {"function": {"name": "personal_items"}},
        ])
        with patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool("delegate_task", {"task": "explore"})
        self.assertEqual(seen["tools"], {"local_shell"})
        self.assertNotIn("personal_items", seen["clients"])

    def test_skill_scoped_subturn_offers_it_explicitly(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients,
                           max_tool_rounds=25, **kw):
            seen["tools"] = {t["function"]["name"] for t in tools or []}
            seen["clients"] = set(builtin_clients)
            return "done", {}

        client = self._delegate([
            {"function": {"name": "local_shell"}},
            {"function": {"name": "personal_items"}},
        ])
        skill = {
            "name": "todo-groomer", "description": "grooms the list",
            "body": "Review and tidy the personal todo list.",
            "tools": ["personal_items"], "model": "", "provider": "",
            "rounds": 0,
        }
        with patch("conch.skills.get_skill", return_value=skill), \
             patch("conch.runtime.chat_turn", fake_chat_turn), \
             patch("sys.stderr", io.StringIO()):
            client.call_tool(
                "delegate_task",
                {"task": "groom the list", "skill": "todo-groomer"},
            )
        self.assertEqual(seen["tools"], {"personal_items"})
        self.assertIn("personal_items", seen["clients"])


class TestFleetWorkerSurface(ItemsToolCase):
    """Fleet workers see personal_items only when the task envelope
    names it — the envelope is the explicit offer."""

    def _executor(self, tools):
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
        from conch.fleet.taskexec import TaskExecutor

        return TaskExecutor(home, task_id, 1)

    def test_not_in_worker_denylist(self):
        from conch.fleet.taskexec import WORKER_TOOL_DENYLIST

        self.assertNotIn("personal_items", WORKER_TOOL_DENYLIST)

    def test_absent_unless_envelope_offers(self):
        with patch("conch.tooling.discover_user_tools",
                   return_value=([], object())):
            executor = self._executor(["local_shell"])
            clients, tools = executor._build_tools(
                {"provider": "openai"}
            )
        self.assertNotIn("personal_items", clients)
        self.assertNotIn(
            "personal_items",
            {t["function"]["name"] for t in tools},
        )

    def test_present_when_envelope_offers(self):
        with patch("conch.tooling.discover_user_tools",
                   return_value=([], object())):
            executor = self._executor(["personal_items"])
            clients, tools = executor._build_tools(
                {"provider": "openai"}
            )
        self.assertIn("personal_items", clients)
        self.assertIn(
            "personal_items",
            {t["function"]["name"] for t in tools},
        )


if __name__ == "__main__":
    unittest.main()
