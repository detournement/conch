"""Startup never runs on a silently empty tool set.

Observed: the shell waited 30s for the background MCP loader and, when it
had not finished, adopted ``ToolRuntimeState(all_tools=[], ...)`` for the
rest of the session — no local_shell, no warning — while the system
prompt kept telling the model to run commands with local_shell. The
loader's result, arriving seconds later, was never swapped in.

``ToolLoader`` gives the session the builtin tools immediately, names
what is still loading or unreachable, and hands the full state over at a
safe point (between turns) with ``needs_tool_refresh`` set.
"""

import contextlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import bootstrap
from conch.bootstrap import (
    TOOL_LOAD_WAIT_SECONDS,
    ToolLoader,
    load_runtime_tools,
    make_builtin_clients,
    swap_tool_state,
    tool_status_lines,
)
from conch.commands import handle_slash_command
from conch.mcp import ServerStatus
from conch.memory import MemoryStore
from conch.session import AgentSession
from conch.tooling import ToolRuntimeState, default_permissions, set_agent_mode


def _tool(name):
    return {"type": "function", "function": {
        "name": name, "description": f"{name} tool",
        "parameters": {"type": "object", "properties": {}}}}


class FakeServer:
    """An MCP client whose list_tools blocks until released (or fails)."""

    def __init__(self, name, tools=(), *, release=None, unreachable="", raises=None):
        self.name = name
        self._tools = list(tools)
        self._release = release
        self.unreachable = bool(unreachable)
        self.last_error = unreachable
        self._raises = raises
        self.closed = False

    def list_tools(self):
        if self._release is not None:
            self._release.wait(10)
        if self._raises is not None:
            raise self._raises
        if self.unreachable:
            return []
        return list(self._tools)

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": f"{self.name}:{name}"}]}

    def close(self):
        self.closed = True


class LoaderCase(unittest.TestCase):
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
        self.config = {"provider": "openai", "chat_model": "gpt-4o"}
        self.memory = MemoryStore()
        self.builtins = make_builtin_clients(
            self.memory, self.config, interactive=False, permissions=default_permissions()
        )
        # Nothing here reads the user's mcp.json or spawns a server.
        p = patch.object(bootstrap.mcp_mod, "save_tool_cache", lambda tools: None)
        p.start()
        self.addCleanup(p.stop)

    def _loader(self, servers, names=None):
        names = list(servers) if names is None else names
        patches = [
            patch.object(bootstrap.mcp_mod, "create_clients", lambda: dict(servers)),
            patch.object(bootstrap.mcp_mod, "configured_server_names", lambda config: list(names)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ToolLoader(self.builtins, self.config)

    @staticmethod
    def _names(tools):
        return [t["function"]["name"] for t in tools]


class TestBuiltinsFirst(LoaderCase):
    def test_builtin_state_is_usable_before_the_loader_starts(self):
        loader = self._loader({"slow": FakeServer("slow", release=threading.Event())})
        names = self._names(loader.builtin_state.all_tools)
        for builtin in ("local_shell", "interactive_terminal", "ssh_remote",
                        "manage_tools", "save_memory", "conch_config", "public_api",
                        "search_conversations", "todo_list", "personal_items",
                        "delegate_task", "skill_manage", "conch_introspect"):
            self.assertIn(builtin, names)
        self.assertIs(loader.builtin_state.tool_map["local_shell"], self.builtins["local_shell"])
        self.assertEqual(loader.builtin_state.server_status["slow"].state, "loading")
        self.assertEqual(loader.pending(), ["slow"])

    def test_wait_default_is_thirty_seconds(self):
        self.assertEqual(TOOL_LOAD_WAIT_SECONDS, 30.0)


class TestLateAttach(LoaderCase):
    def test_loader_finishing_after_the_wait_attaches_between_turns(self):
        release = threading.Event()
        slow = FakeServer("slow", [_tool("qdrant_find")], release=release)
        quick = FakeServer("quick", [_tool("jira_search")])
        loader = self._loader({"slow": slow, "quick": quick}).start()
        live = loader.builtin_state
        builtin_count = len(live.all_tools)

        # The startup wait expires with one server still loading.
        self.assertFalse(loader.wait(timeout=0.2))
        self.assertIsNone(loader.adopt(live))
        self.assertIn("local_shell", self._names(live.tools))
        self.assertNotIn("qdrant_find", self._names(live.all_tools))
        self.assertEqual(loader.pending(), ["slow"])
        self.assertEqual(
            loader.notice_lines(builtin_count),
            [f"tools: {builtin_count} builtin ready; 1 MCP server(s) still loading: slow"
             " — they attach when ready"],
        )

        release.set()
        self.assertTrue(loader.wait(timeout=5))
        clients = loader.adopt(live)
        self.assertEqual(set(clients), {"slow", "quick"})
        names = self._names(live.all_tools)
        self.assertIn("qdrant_find", names)
        self.assertIn("jira_search", names)
        self.assertIn("local_shell", names)
        self.assertTrue(live.needs_tool_refresh)
        self.assertEqual(live.server_status["slow"].state, "ready")
        self.assertEqual(live.server_status["quick"].tool_count, 1)
        self.assertEqual(loader.notice_lines(len(live.all_tools)), [])
        # Second adopt is a no-op: the swap happens exactly once.
        self.assertIsNone(loader.adopt(live))

    def test_first_turn_has_builtins_later_turn_has_the_full_list(self):
        release = threading.Event()
        loader = self._loader({"slow": FakeServer("slow", [_tool("qdrant_find")], release=release)}).start()
        live = loader.builtin_state
        session = AgentSession(self.config, interactive=False, permissions=default_permissions(),
                               memory=self.memory)
        session.attach_clients(self.builtins, chat_state=live, mcp_clients={})
        self.addCleanup(session.close)
        seen = []

        def fake_chat_turn(config, provider, raw_fn, messages, tools, tool_map, builtin_clients,
                           max_tool_rounds=25, chat_state=None, on_token=None, input_fn=None):
            seen.append({
                "tools": [t["function"]["name"] for t in (tools or [])],
                "refresh": bool(chat_state.needs_tool_refresh),
                "has_qdrant_client": "qdrant_find" in tool_map,
            })
            # runtime.chat_turn consumes the flag at its first request.
            chat_state.needs_tool_refresh = False
            return "ok", {"input_tokens": 1, "output_tokens": 1, "model": "m"}

        with patch("conch.runtime.chat_turn", fake_chat_turn):
            self.assertFalse(loader.wait(timeout=0.1))
            loader.adopt(live)  # still loading: no swap
            session.run_turn([{"role": "user", "content": "first"}])
            release.set()
            self.assertTrue(loader.wait(timeout=5))
            self.assertIsNotNone(loader.adopt(live))
            session.run_turn([{"role": "user", "content": "second"}])

        first, second = seen
        self.assertIn("local_shell", first["tools"])
        self.assertNotIn("qdrant_find", first["tools"])
        self.assertFalse(first["has_qdrant_client"])
        self.assertIn("qdrant_find", second["tools"])
        self.assertIn("local_shell", second["tools"])
        self.assertTrue(second["refresh"])
        self.assertTrue(second["has_qdrant_client"])
        self.assertFalse(live.needs_tool_refresh)

    def test_loaded_in_time_is_the_old_fast_path(self):
        loader = self._loader({"quick": FakeServer("quick", [_tool("jira_search")])}).start()
        live = loader.builtin_state
        self.assertTrue(loader.wait(timeout=5))
        self.assertIsNotNone(loader.adopt(live))
        self.assertIn("jira_search", self._names(live.tools))
        self.assertEqual(loader.notice_lines(len(live.all_tools)), [])


class TestUnreachableServers(LoaderCase):
    def test_unreachable_server_is_named_once_and_the_others_load(self):
        dead = FakeServer("qdrant", unreachable="timed out after 5s")
        live_srv = FakeServer("docs", [_tool("search_capitol_docs")])
        loader = self._loader({"qdrant": dead, "docs": live_srv}).start()
        live = loader.builtin_state
        self.assertTrue(loader.wait(timeout=5))
        self.assertIsNotNone(loader.adopt(live))
        self.assertIn("search_capitol_docs", self._names(live.all_tools))
        self.assertEqual(live.server_status["qdrant"].describe(), "unreachable: timed out after 5s")
        lines = loader.notice_lines(len(live.all_tools))
        self.assertEqual(lines, [
            "tools: MCP server qdrant unreachable: timed out after 5s"
            " (/tools for status, /reload to retry)"
        ])
        self.assertEqual(loader.notice_lines(len(live.all_tools), already_reported={"qdrant"}), [])

    def test_still_loading_and_failed_lines_together(self):
        statuses = {
            "qdrant": ServerStatus("qdrant", "unreachable", "timed out after 5s"),
            "jira": ServerStatus("jira"),
            "filesystem": ServerStatus("filesystem"),
            "docs": ServerStatus("docs", "ready", "", 4, 0.3),
            "broken": ServerStatus("broken", "error", "HTTP 500: upstream"),
        }
        self.assertEqual(tool_status_lines(statuses, 15), [
            "tools: MCP server qdrant unreachable: timed out after 5s (/tools for status, /reload to retry)",
            "tools: MCP server broken error: HTTP 500: upstream (/tools for status, /reload to retry)",
            "tools: 15 builtin ready; 2 MCP server(s) still loading: jira, filesystem — they attach when ready",
        ])

    def test_loader_crash_is_loud_and_leaves_builtins_usable(self):
        def explode():
            raise ValueError("mcp.json: server 'docs' has connect_timeout='soon'; it must be a positive number of seconds")

        p = patch.object(bootstrap.mcp_mod, "create_clients", explode)
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(bootstrap.mcp_mod, "configured_server_names", lambda config: ["docs"])
        q.start()
        self.addCleanup(q.stop)
        loader = ToolLoader(self.builtins, self.config).start()
        live = loader.builtin_state
        self.assertTrue(loader.wait(timeout=5))
        self.assertIsNone(loader.adopt(live))
        self.assertIsInstance(loader.error, ValueError)
        self.assertIn("local_shell", self._names(live.tools))
        self.assertEqual(loader.statuses()["docs"].state, "error")
        [line] = loader.notice_lines(len(live.all_tools))
        self.assertTrue(line.startswith("tools: MCP discovery failed: mcp.json: server 'docs'"))
        self.assertIn("builtin tools only (/reload to retry)", line)


class TestSupersede(LoaderCase):
    def test_a_superseded_loader_never_attaches_and_closes_its_clients(self):
        release = threading.Event()
        slow = FakeServer("slow", [_tool("late_tool")], release=release)
        loader = self._loader({"slow": slow}).start()
        live = loader.builtin_state
        self.assertFalse(loader.wait(timeout=0.1))
        loader.supersede()
        release.set()
        self.assertTrue(loader.wait(timeout=5))
        self.assertIsNone(loader.adopt(live))
        self.assertNotIn("late_tool", self._names(live.all_tools))
        self.assertTrue(slow.closed)

    def test_superseding_a_finished_unadopted_loader_closes_now(self):
        quick = FakeServer("quick", [_tool("t")])
        loader = self._loader({"quick": quick}).start()
        self.assertTrue(loader.wait(timeout=5))
        loader.supersede()
        self.assertTrue(quick.closed)
        self.assertIsNone(loader.adopt(loader.builtin_state))


class TestSwapAndSyncPath(LoaderCase):
    def test_swap_keeps_the_live_object_and_sets_the_refresh_flag(self):
        live = ToolRuntimeState(all_tools=[_tool("a")], tool_map={"a": object()}, tools=[_tool("a")])
        live.pinned_tools.add("a")
        loaded = ToolRuntimeState(all_tools=[_tool("b")], tool_map={"b": object()}, tools=[_tool("b")],
                                  server_status={"s": ServerStatus("s", "ready", "", 1, 0.1)})
        swap_tool_state(live, loaded)
        self.assertEqual(self._names(live.all_tools), ["b"])
        self.assertIs(live.tool_map, loaded.tool_map)
        self.assertTrue(live.needs_tool_refresh)
        self.assertEqual(live.pinned_tools, {"a"})  # session state survives the swap
        self.assertEqual(live.server_status["s"].state, "ready")

    def test_load_runtime_tools_records_server_status(self):
        servers = {
            "qdrant": FakeServer("qdrant", unreachable="timed out after 5s"),
            "docs": FakeServer("docs", [_tool("search_capitol_docs")]),
        }
        with patch.object(bootstrap.mcp_mod, "create_clients", lambda: dict(servers)):
            clients, state = load_runtime_tools(self.builtins, config=self.config)
        self.assertEqual(set(clients), {"qdrant", "docs"})
        self.assertEqual(state.server_status["qdrant"].state, "unreachable")
        self.assertEqual(state.server_status["docs"].state, "ready")
        self.assertEqual(state.server_status["docs"].tool_count, 1)
        self.assertIn("search_capitol_docs", self._names(state.all_tools))
        self.assertIn("local_shell", self._names(state.all_tools))


class TestToolsCommandStatus(unittest.TestCase):
    def _run(self, server_status):
        all_tools = [_tool("local_shell"), _tool("search_capitol_docs")]

        class Docs:
            name = "docs"

        tool_map = {"local_shell": object(), "search_capitol_docs": Docs()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handle_slash_command("/tools", {}, "openai", "m", lambda v: None,
                                          all_tools=all_tools, tool_map=tool_map,
                                          server_status=server_status)
        self.assertIsNone(result)
        return out.getvalue()

    def test_per_server_status_is_listed(self):
        statuses = {
            "docs": ServerStatus("docs", "ready", "", 1, 0.4),
            "qdrant": ServerStatus("qdrant", "unreachable", "timed out after 5s"),
            "jira": ServerStatus("jira"),
        }
        output = self._run(lambda: statuses)
        self.assertIn("MCP servers:", output)
        self.assertIn("ready", output)
        self.assertIn("qdrant", output)
        self.assertIn("unreachable: timed out after 5s", output)
        self.assertIn("loading", output)
        self.assertIn("Tool groups:", output)

    def test_without_status_the_listing_is_unchanged(self):
        output = self._run(None)
        self.assertIn("Tool groups:", output)
        self.assertNotIn("MCP servers:", output)


if __name__ == "__main__":
    unittest.main()
