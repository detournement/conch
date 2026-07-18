"""Tests for the remote agentic loop (plan 4.3): session mapping, the
safe_auto permission cap, approval-over-channel flow, tool exclusions, and
scheduled-output routing."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.channels import InboundMessage
from conch.remote import (
    REMOTE_EXCLUDED_TOOLS,
    ApprovalStore,
    RemoteLoop,
    RemoteShellClient,
)
from conch.tooling import ToolRuntimeState, set_agent_mode


class RemoteTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)


class TestApprovalStore(RemoteTestCase):
    def test_add_pop_roundtrip(self):
        store = ApprovalStore()
        rid = store.add("touch /tmp/x", "slack", "99.1")
        self.assertEqual(rid, 1)
        entry = store.pop(rid)
        self.assertEqual(entry["command"], "touch /tmp/x")
        self.assertEqual(entry["channel"], "slack")
        self.assertIsNone(store.pop(rid), "pop consumes the request")

    def test_ids_increment_and_persist(self):
        store = ApprovalStore()
        store.add("a", "sms", "t")
        rid = ApprovalStore().add("b", "sms", "t")  # fresh handle, same file
        self.assertEqual(rid, 2)
        self.assertEqual(len(ApprovalStore().pending()), 2)


class _Notifications:
    def __init__(self):
        self.sent = []

    def __call__(self, text, thread_id):
        self.sent.append((text, thread_id))
        return True, ""


class TestRemoteShellClient(RemoteTestCase):
    def _client(self):
        notify = _Notifications()
        client = RemoteShellClient(ApprovalStore(), notify, "slack", "99.1")
        return client, notify

    def test_safe_command_runs(self):
        client, notify = self._client()
        with patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            result = client.call_tool("local_shell", {"command": "echo remote-safe"})
        self.assertIn("remote-safe", result["content"][0]["text"])
        self.assertEqual(notify.sent, [])

    def test_mutating_command_needs_approval(self):
        client, notify = self._client()
        result = client.call_tool("local_shell", {"command": "touch /tmp/remote-x"})
        text = result["content"][0]["text"]
        self.assertIn("requires user approval", text)
        self.assertIn("#1", text)
        self.assertEqual(len(notify.sent), 1)
        self.assertIn("approve 1", notify.sent[0][0])
        self.assertIn("touch /tmp/remote-x", notify.sent[0][0])

    def test_destructive_never_auto_runs_even_in_agent_mode(self):
        set_agent_mode(True)
        client, notify = self._client()
        result = client.call_tool("local_shell", {"command": "rm -rf /tmp/scratch"})
        self.assertIn("requires user approval", result["content"][0]["text"])

    def test_agent_mode_does_not_lift_remote_cap(self):
        set_agent_mode(True)
        client, notify = self._client()
        result = client.call_tool("local_shell", {"command": "pip install requests"})
        self.assertIn("requires user approval", result["content"][0]["text"],
                      "remote sessions stay capped at safe_auto in agent mode")


def _msg(text, sender="U111", thread="99.1", channel="slack"):
    return InboundMessage(channel=channel, sender=sender, text=text, thread_id=thread)


class _FakeConvManager:
    def __init__(self):
        self.saved = []
        self._convs = {}

    def create(self, model, provider):
        import types
        conv = types.SimpleNamespace(
            id=f"conv{len(self._convs) + 1}", title="", model=model,
            provider=provider, messages=[],
        )
        self._convs[conv.id] = conv
        return conv

    def load(self, conv_id):
        return self._convs.get(conv_id)

    def save(self, conv):
        self.saved.append(conv.id)


class TestRemoteLoopTurns(RemoteTestCase):
    def _loop(self, chat_turn_fn, config=None):
        config = config or {"provider": "ollama", "chat_model": "qwen3.6:27b",
                            "model": "qwen3.6:27b"}
        conv_mgr = _FakeConvManager()
        state = ToolRuntimeState(
            all_tools=[], tool_map={},
            tools=[{"function": {"name": n}} for n in
                   ("local_shell", "delegate_task", "conch_config", "public_api")],
        )
        loop = RemoteLoop(config, conv_mgr=conv_mgr, chat_state=state,
                          builtin_clients={"local_shell": object(),
                                           "delegate_task": object(),
                                           "conch_config": object()})
        sent = []
        loop.manager.notify = lambda text, channel="", thread_id="": (
            sent.append((text, channel, thread_id)) or (True, "")
        )
        return loop, conv_mgr, sent, chat_turn_fn

    def test_inbound_maps_to_conversation_and_replies(self):
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, max_tool_rounds=25, **kw):
            seen["messages"] = list(messages)
            seen["tools"] = tools
            seen["clients"] = builtin_clients
            return "disk is 42% full", {"input_tokens": 1, "output_tokens": 1}

        loop, conv_mgr, sent, _ = self._loop(fake_chat_turn)
        with patch("conch.remote.chat_turn", fake_chat_turn, create=True), \
             patch("conch.runtime.chat_turn", fake_chat_turn):
            reply = loop.handle_inbound(_msg("how full is the disk?"))
        self.assertIn("disk is 42% full", reply)
        # reply went back over the same thread
        self.assertEqual(sent[-1][2], "99.1")
        # conversation persisted with the user + assistant turns
        self.assertEqual(conv_mgr.saved, ["conv1"])
        roles = [m["role"] for m in conv_mgr._convs["conv1"].messages]
        self.assertEqual(roles, ["system", "user", "assistant"])
        # remote sessions never see excluded tools
        tool_names = {t["function"]["name"] for t in seen["tools"]}
        self.assertEqual(tool_names, {"local_shell", "public_api"})
        self.assertNotIn("delegate_task", seen["clients"])
        self.assertNotIn("conch_config", seen["clients"])
        self.assertIsInstance(seen["clients"]["local_shell"], RemoteShellClient)

    def test_same_thread_resumes_same_conversation(self):
        def fake_chat_turn(config, provider, raw_fn, messages, *a, **kw):
            return "ok", {}

        loop, conv_mgr, sent, _ = self._loop(fake_chat_turn)
        with patch("conch.runtime.chat_turn", fake_chat_turn):
            loop.handle_inbound(_msg("first"))
            loop.handle_inbound(_msg("second"))
        self.assertEqual(len(conv_mgr._convs), 1, "thread == conversation")
        texts = [m["content"] for m in conv_mgr._convs["conv1"].messages
                 if m["role"] == "user"]
        self.assertEqual(texts, ["first", "second"])

    def test_different_threads_get_different_conversations(self):
        def fake_chat_turn(config, provider, raw_fn, messages, *a, **kw):
            return "ok", {}

        loop, conv_mgr, sent, _ = self._loop(fake_chat_turn)
        with patch("conch.runtime.chat_turn", fake_chat_turn):
            loop.handle_inbound(_msg("a", thread="99.1"))
            loop.handle_inbound(_msg("b", thread="77.7"))
        self.assertEqual(len(conv_mgr._convs), 2)

    def test_backend_error_produces_clean_channel_message(self):
        def fake_chat_turn(config, provider, raw_fn, messages, *a, **kw):
            return "", {"error": "connection refused"}

        loop, conv_mgr, sent, _ = self._loop(fake_chat_turn)
        with patch("conch.runtime.chat_turn", fake_chat_turn):
            reply = loop.handle_inbound(_msg("hello?"))
        self.assertIn("unreachable", reply)
        self.assertNotIn("[API error", reply)


class TestApprovalFlow(RemoteTestCase):
    def _loop(self):
        config = {"provider": "ollama", "chat_model": "qwen3.6:27b"}
        loop = RemoteLoop(config, conv_mgr=_FakeConvManager(),
                          chat_state=ToolRuntimeState(all_tools=[], tool_map={}, tools=[]),
                          builtin_clients={})
        sent = []
        loop.manager.notify = lambda text, channel="", thread_id="": (
            sent.append(text) or (True, "")
        )
        return loop, sent

    def test_approve_runs_pending_command(self):
        loop, sent = self._loop()
        rid = loop.approvals.add("echo approved-output", "slack", "99.1")
        with patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            reply = loop.handle_inbound(_msg(f"approve {rid}"))
        self.assertIn("approved-output", reply)
        self.assertIsNone(loop.approvals.pop(rid), "approval consumed")

    def test_deny_discards_command(self):
        loop, sent = self._loop()
        rid = loop.approvals.add("touch /tmp/x", "slack", "99.1")
        reply = loop.handle_inbound(_msg(f"deny {rid}"))
        self.assertIn("Denied", reply)
        self.assertIsNone(loop.approvals.pop(rid))

    def test_unknown_approval_id(self):
        loop, sent = self._loop()
        reply = loop.handle_inbound(_msg("approve 999"))
        self.assertIn("No pending approval", reply)


class TestScheduledOutputRouting(RemoteTestCase):
    def _task(self):
        import types
        return types.SimpleNamespace(id=7, prompt="check disk")

    def test_output_routed_to_notify_channel(self):
        from conch.app import _route_scheduled_output
        sent = []

        class FakeManager:
            def __init__(self, config):
                pass

            def notify(self, text, channel="", thread_id=""):
                sent.append(text)
                return True, ""

        config = {"notify_channel": "slack"}
        with patch("conch.app.ChannelManager", FakeManager, create=True), \
             patch("conch.channels.ChannelManager", FakeManager):
            _route_scheduled_output(config, self._task(), "disk at 42%", {})
        self.assertEqual(len(sent), 1)
        self.assertIn("#7", sent[0])
        self.assertIn("check disk", sent[0])
        self.assertIn("disk at 42%", sent[0])

    def test_no_channel_configured_is_noop(self):
        from conch.app import _route_scheduled_output
        with patch("conch.channels.ChannelManager",
                   side_effect=AssertionError("must not construct a manager")):
            _route_scheduled_output({}, self._task(), "output", {})

    def test_backend_error_becomes_clean_message(self):
        from conch.app import _route_scheduled_output
        sent = []

        class FakeManager:
            def __init__(self, config):
                pass

            def notify(self, text, channel="", thread_id=""):
                sent.append(text)
                return True, ""

        with patch("conch.channels.ChannelManager", FakeManager):
            _route_scheduled_output({"notify_channel": "slack"}, self._task(),
                                    "", {"error": "connection refused"})
        self.assertIn("unreachable", sent[0])

    def test_notify_failure_never_raises(self):
        from conch.app import _route_scheduled_output

        class ExplodingManager:
            def __init__(self, config):
                raise RuntimeError("boom")

        with patch("conch.channels.ChannelManager", ExplodingManager), \
             patch("sys.stderr", io.StringIO()):
            _route_scheduled_output({"notify_channel": "slack"}, self._task(), "x", {})


class TestRemoteTools(RemoteTestCase):
    def test_excluded_tools_constant_covers_dangerous_set(self):
        for name in ("delegate_task", "conch_config", "manage_tools", "skill_manage"):
            self.assertIn(name, REMOTE_EXCLUDED_TOOLS)


if __name__ == "__main__":
    unittest.main()
