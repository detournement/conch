"""The shell reaches its prompt without waiting on the network.

* MCP: tool schemas are cached per server, keyed on a fingerprint of that
  server's mcp.json block (never on credential values). With a matching
  cache the first-moment tool state already offers the server's tools and
  the shell does not wait; a call made before the server connected waits
  for it inside dispatch, and a tool the server no longer offers comes
  back as a clear tool error. Headless sessions use the cache without
  starting or listing any server until one of its tools is called.
* Model check: a success is remembered for a day per (provider, model,
  endpoint, key fingerprint); without one the shell checks in the
  background and the first message settles it.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import bootstrap, mcp, modelcheck
from conch.bootstrap import ToolLoader, load_runtime_tools, make_builtin_clients
from conch.memory import MemoryStore
from conch.modelcheck import (
    NO_KEY,
    OK,
    ROUTE_CHECK_KEY,
    TIMEOUT,
    ProbeResult,
    RouteCheck,
    cached_check,
    ensure_working_model,
    remember_check,
    route_fingerprint,
    take_route_check,
    verified_cache_path,
)
from conch.tooling import default_permissions, set_agent_mode


def _tool(name):
    return {"type": "function", "function": {
        "name": name, "description": f"{name} tool",
        "parameters": {"type": "object", "properties": {}}}}


def _names(tools):
    return [t["function"]["name"] for t in tools]


class FakeServer:
    def __init__(self, name, tools=(), *, release=None, unreachable=""):
        self.name = name
        self._tools = list(tools)
        self._release = release
        self.unreachable = bool(unreachable)
        self.last_error = unreachable
        self.listed = 0
        self.calls = []
        self.closed = False

    def list_tools(self):
        self.listed += 1
        if self._release is not None:
            self._release.wait(10)
        return [] if self.unreachable else list(self._tools)

    def call_tool(self, name, arguments):
        self.calls.append(name)
        return {"content": [{"type": "text", "text": f"{self.name}:{name}"}]}

    def close(self):
        self.closed = True


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Tool cache
# ---------------------------------------------------------------------------


class TestServerFingerprint(IsolatedCase):
    def test_credential_values_never_feed_the_key(self):
        base = {"command": "npx", "args": ["-y", "srv"], "env": {"TOKEN": "secret-1"},
                "headers": {"Authorization": "Bearer a"}}
        rotated = dict(base, env={"TOKEN": "secret-2"}, headers={"Authorization": "Bearer b"})
        self.assertEqual(mcp.server_fingerprint(base), mcp.server_fingerprint(rotated))

    def test_command_args_url_and_env_names_change_the_key(self):
        base = {"command": "npx", "args": ["-y", "srv"], "env": {"TOKEN": "x"}}
        prints = {
            mcp.server_fingerprint(base),
            mcp.server_fingerprint(dict(base, args=["-y", "srv@2"])),
            mcp.server_fingerprint(dict(base, command="uvx")),
            mcp.server_fingerprint(dict(base, env={"OTHER": "x"})),
            mcp.server_fingerprint({"type": "http", "url": "http://a/mcp"}),
            mcp.server_fingerprint({"type": "http", "url": "http://b/mcp"}),
        }
        self.assertEqual(len(prints), 6)

    def test_cache_file_holds_no_secret(self):
        cfg = {"command": "srv", "env": {"API_TOKEN": "sk-very-secret"}}
        fingerprint = mcp.server_fingerprint(cfg)
        mcp.save_server_cache({"s": fingerprint}, {"s": [_tool("a")]})
        text = mcp.tool_cache_path().read_text()
        self.assertNotIn("sk-very-secret", text)
        self.assertIn(fingerprint, text)


class TestServerCache(IsolatedCase):
    def test_round_trip_per_server(self):
        mcp.save_server_cache({"a": "fa", "b": "fb"}, {"a": [_tool("t1")], "b": [_tool("t2")]})
        self.assertEqual(
            {k: _names(v) for k, v in mcp.load_server_cache({"a": "fa", "b": "fb"}).items()},
            {"a": ["t1"], "b": ["t2"]},
        )

    def test_changed_config_invalidates_only_that_server(self):
        mcp.save_server_cache({"a": "fa", "b": "fb"}, {"a": [_tool("t1")], "b": [_tool("t2")]})
        found = mcp.load_server_cache({"a": "fa", "b": "fb-changed"})
        self.assertEqual(list(found), ["a"])

    def test_stale_entries_and_other_versions_are_ignored(self):
        mcp.save_server_cache({"a": "fa"}, {"a": [_tool("t1")]})
        self.assertEqual(mcp.load_server_cache({"a": "fa"}, max_age=0), {})
        mcp.tool_cache_path().write_text(json.dumps({"ts": time.time(), "tools": [_tool("t1")]}))
        self.assertEqual(mcp.load_server_cache({"a": "fa"}), {})

    def test_saving_keeps_other_servers_and_drops_unconfigured_ones(self):
        mcp.save_server_cache({"a": "fa", "b": "fb", "gone": "fg"},
                              {"a": [_tool("t1")], "b": [_tool("t2")], "gone": [_tool("t3")]})
        mcp.save_server_cache({"a": "fa", "b": "fb"}, {"a": [_tool("t1"), _tool("t4")]})
        found = mcp.load_server_cache({"a": "fa", "b": "fb", "gone": "fg"})
        self.assertEqual({k: _names(v) for k, v in found.items()},
                         {"a": ["t1", "t4"], "b": ["t2"]})


class TestPendingClient(unittest.TestCase):
    def test_call_waits_for_the_server_then_runs(self):
        pending = mcp.PendingMcpClient("srv", wait_timeout=5)
        real = FakeServer("srv", [_tool("t")])
        threading.Timer(0.1, lambda: pending.resolve(real, [_tool("t")])).start()
        result = pending.call_tool("t", {})
        self.assertEqual(result["content"][0]["text"], "srv:t")
        self.assertEqual(real.calls, ["t"])

    def test_tool_no_longer_offered_is_a_clear_error(self):
        pending = mcp.PendingMcpClient("srv", wait_timeout=1)
        pending.resolve(FakeServer("srv"), [_tool("other")])
        result = pending.call_tool("t", {})
        self.assertTrue(result["isError"])
        self.assertIn("no longer offers t", result["content"][0]["text"])

    def test_unreachable_server_is_a_clear_error(self):
        pending = mcp.PendingMcpClient("srv", wait_timeout=1)
        pending.resolve(FakeServer("srv", unreachable="timed out"), [], "timed out after 5s")
        text = pending.call_tool("t", {})["content"][0]["text"]
        self.assertIn("MCP server srv is unavailable: timed out after 5s", text)

    def test_wait_is_bounded(self):
        pending = mcp.PendingMcpClient("srv", wait_timeout=0.05)
        result = pending.call_tool("t", {})
        self.assertTrue(result["isError"])
        self.assertIn("still connecting", result["content"][0]["text"])

    def test_lazy_connect_happens_once_on_first_call_and_close_closes_it(self):
        real = FakeServer("srv", [_tool("t")])
        made = []

        def connect():
            made.append(1)
            return real

        pending = mcp.PendingMcpClient("srv", connect=connect)
        self.assertEqual(made, [])
        pending.call_tool("t", {})
        pending.call_tool("t", {})
        self.assertEqual(made, [1])
        self.assertEqual(real.listed, 1)
        pending.close()
        self.assertTrue(real.closed)

    def test_execute_tool_reports_the_pending_error_text(self):
        pending = mcp.PendingMcpClient("srv", wait_timeout=0.01)
        text = mcp.execute_tool({"t": pending}, "t", {})
        self.assertTrue(text.startswith("Error: MCP server srv is still connecting"))


class LoaderCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)
        self.config = {"provider": "openai", "chat_model": "gpt-4o"}
        self.builtins = make_builtin_clients(
            MemoryStore(), self.config, interactive=False, permissions=default_permissions()
        )

    def _configure(self, servers, cfgs):
        """Pretend mcp.json names *cfgs* (name -> block); create_clients
        returns *servers*."""
        fingerprints = {name: mcp.server_fingerprint(cfg) for name, cfg in cfgs.items()}
        for p in (
            patch.object(bootstrap.mcp_mod, "create_clients", lambda: dict(servers)),
            patch.object(bootstrap.mcp_mod, "configured_server_names", lambda config: list(cfgs)),
            patch.object(bootstrap.mcp_mod, "server_fingerprints", lambda config: dict(fingerprints)),
            patch.object(bootstrap.mcp_mod, "server_configs", lambda config: dict(cfgs)),
        ):
            p.start()
            self.addCleanup(p.stop)
        return fingerprints


class TestLoaderWithCache(LoaderCase):
    def test_cached_tools_are_offered_at_once_and_calls_wait_for_the_server(self):
        release = threading.Event()
        slow = FakeServer("slow", [_tool("slow_a"), _tool("slow_b")], release=release)
        quick = FakeServer("quick", [_tool("quick_a")])
        prints = self._configure({"slow": slow, "quick": quick},
                                 {"slow": {"command": "slow"}, "quick": {"command": "quick"}})
        mcp.save_server_cache(prints, {"slow": [_tool("slow_a"), _tool("slow_b")],
                                       "quick": [_tool("quick_a")]})
        loader = ToolLoader(self.builtins, self.config)
        self.assertEqual(loader.startup_wait(), 0.0)
        live = loader.builtin_state
        initial = _names(live.all_tools)
        self.assertLess(initial.index("slow_a"), initial.index("quick_a"))
        self.assertIn("local_shell", initial)
        loader.start()

        # A call before the slow server answered waits for it in dispatch.
        threading.Timer(0.1, release.set).start()
        text = mcp.execute_tool(live.tool_map, "slow_b", {})
        self.assertEqual(text, "slow:slow_b")
        self.assertEqual(loader.notice_lines(len(live.all_tools)), [])

        self.assertTrue(loader.wait(timeout=5))
        self.assertIsNotNone(loader.adopt(live))
        # Same list, same order, now routed to the real clients.
        self.assertEqual(_names(live.all_tools), initial)
        self.assertIs(live.tool_map["slow_a"], slow)

    def test_partial_cache_waits_briefly_and_lists_the_uncached_server_as_loading(self):
        release = threading.Event()
        self.addCleanup(release.set)
        prints = self._configure(
            {"a": FakeServer("a", [_tool("a1")]), "b": FakeServer("b", [_tool("b1")], release=release)},
            {"a": {"command": "a"}, "b": {"command": "b"}},
        )
        mcp.save_server_cache(prints, {"a": [_tool("a1")]})
        loader = ToolLoader(self.builtins, self.config)
        self.assertEqual(loader.startup_wait(), bootstrap.TOOL_LOAD_WAIT_SECONDS)
        self.assertIn("a1", _names(loader.builtin_state.all_tools))
        self.assertNotIn("b1", _names(loader.builtin_state.all_tools))
        loader.start()
        lines = loader.notice_lines(10)
        self.assertEqual(len(lines), 1)
        self.assertIn("still loading: b", lines[0])

    def test_tool_dropped_by_the_server_is_a_tool_error_then_disappears(self):
        prints = self._configure({"s": FakeServer("s", [_tool("kept")])}, {"s": {"command": "s"}})
        mcp.save_server_cache(prints, {"s": [_tool("kept"), _tool("dropped")]})
        loader = ToolLoader(self.builtins, self.config).start()
        live = loader.builtin_state
        self.assertTrue(loader.wait(timeout=5))
        text = mcp.execute_tool(live.tool_map, "dropped", {})
        self.assertIn("no longer offers dropped", text)
        loader.adopt(live)
        self.assertNotIn("dropped", _names(live.all_tools))

    def test_discovery_refreshes_the_cache_with_answering_servers_only(self):
        saved = []
        self._configure(
            {"up": FakeServer("up", [_tool("u")]),
             "down": FakeServer("down", unreachable="refused")},
            {"up": {"command": "up"}, "down": {"command": "down"}},
        )
        with patch.object(bootstrap.mcp_mod, "save_server_cache",
                          lambda prints, results: saved.append((dict(prints), results))):
            loader = ToolLoader(self.builtins, self.config).start()
            self.assertTrue(loader.wait(timeout=5))
        [(prints, results)] = saved
        self.assertEqual(set(prints), {"up", "down"})
        self.assertEqual({k: _names(v) for k, v in results.items()}, {"up": ["u"]})

    def test_loader_crash_releases_waiting_calls(self):
        prints = self._configure({}, {"s": {"command": "s"}})
        mcp.save_server_cache(prints, {"s": [_tool("t")]})

        def explode():
            raise ValueError("bad mcp.json")

        with patch.object(bootstrap.mcp_mod, "create_clients", explode):
            loader = ToolLoader(self.builtins, self.config).start()
            self.assertTrue(loader.wait(timeout=5))
        text = mcp.execute_tool(loader.builtin_state.tool_map, "t", {})
        self.assertIn("discovery failed: bad mcp.json", text)


class TestHeadlessCache(LoaderCase):
    def test_full_cache_starts_and_lists_nothing_until_a_tool_is_called(self):
        real = FakeServer("s", [_tool("t")])
        cfgs = {"s": {"command": "s"}}
        prints = self._configure({}, cfgs)
        mcp.save_server_cache(prints, {"s": [_tool("t")]})

        def no_live(*_a, **_k):
            raise AssertionError("headless session relisted tools")

        created = []
        with patch.object(bootstrap.mcp_mod, "create_clients", no_live), \
                patch.object(bootstrap.mcp_mod, "ToolDiscovery", no_live), \
                patch.object(bootstrap.mcp_mod, "create_client",
                             lambda name, cfg: created.append((name, cfg)) or real):
            clients, state = load_runtime_tools(self.builtins, config=self.config)
            self.assertIn("t", _names(state.all_tools))
            self.assertEqual(state.server_status["s"].state, "cached")
            self.assertEqual(created, [])
            self.assertEqual(mcp.execute_tool(state.tool_map, "t", {}), "s:t")
        self.assertEqual(created, [("s", cfgs["s"])])
        mcp.close_all(clients)
        self.assertTrue(real.closed)

    def test_partial_cache_falls_back_to_live_discovery(self):
        servers = {"a": FakeServer("a", [_tool("a1")]), "b": FakeServer("b", [_tool("b1")])}
        prints = self._configure(servers, {"a": {"command": "a"}, "b": {"command": "b"}})
        mcp.save_server_cache(prints, {"a": [_tool("a1")]})
        clients, state = load_runtime_tools(self.builtins, config=self.config)
        self.assertIs(clients["a"], servers["a"])
        self.assertEqual(servers["b"].listed, 1)
        self.assertIn("b1", _names(state.all_tools))
        # ...and the cache now covers both.
        self.assertEqual(set(mcp.load_server_cache(prints)), {"a", "b"})

    def test_use_cache_false_always_discovers(self):
        servers = {"a": FakeServer("a", [_tool("a1")])}
        prints = self._configure(servers, {"a": {"command": "a"}})
        mcp.save_server_cache(prints, {"a": [_tool("a1")]})
        clients, _state = load_runtime_tools(self.builtins, use_cache=False, config=self.config)
        self.assertEqual(servers["a"].listed, 1)

    def test_build_agent_session_uses_the_cache_by_default(self):
        prints = self._configure({}, {"s": {"command": "s"}})
        mcp.save_server_cache(prints, {"s": [_tool("t")]})
        with patch.object(bootstrap.mcp_mod, "create_clients",
                          side_effect=AssertionError("relisted")):
            session = bootstrap.build_agent_session(self.config)
        self.addCleanup(session.close)
        self.assertIn("t", _names(session.chat_state.all_tools))


# ---------------------------------------------------------------------------
# Model check
# ---------------------------------------------------------------------------


class ModelCacheCase(IsolatedCase):
    def setUp(self):
        super().setUp()
        p = patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-one"})
        p.start()
        self.addCleanup(p.stop)
        self.config = {"provider": "openai", "chat_model": "gpt-4o", "model": "gpt-4o"}


class TestRouteFingerprint(ModelCacheCase):
    def test_key_rotation_endpoint_and_model_change_the_fingerprint(self):
        first = route_fingerprint("openai", "gpt-4o", self.config)
        self.assertEqual(first, route_fingerprint("openai", "gpt-4o", self.config))
        self.assertNotEqual(first, route_fingerprint("openai", "gpt-4.1", self.config))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-two"}):
            self.assertNotEqual(first, route_fingerprint("openai", "gpt-4o", self.config))
        with patch.dict(os.environ, {"CONCH_OPENAI_API_BASE": "http://127.0.0.1:9/v1"}):
            self.assertNotEqual(first, route_fingerprint("openai", "gpt-4o", self.config))

    def test_cache_file_never_holds_the_key(self):
        remember_check(ProbeResult("openai", "gpt-4o", OK), self.config)
        text = verified_cache_path().read_text()
        self.assertNotIn("sk-test-key-one", text)
        self.assertIsNotNone(cached_check("openai", "gpt-4o", self.config))

    def test_failures_are_not_remembered_and_entries_expire(self):
        remember_check(ProbeResult("openai", "gpt-4o", TIMEOUT, "x"), self.config)
        self.assertIsNone(cached_check("openai", "gpt-4o", self.config))
        remember_check(ProbeResult("openai", "gpt-4o", OK), self.config)
        self.assertIsNone(cached_check("openai", "gpt-4o", self.config, ttl=0))


class TestEnsureWorkingModelCache(ModelCacheCase):
    def test_success_is_remembered_and_the_next_launch_sends_no_probe(self):
        ok = ProbeResult("openai", "gpt-4o", OK, "listed")
        with patch.object(modelcheck, "probe_model", return_value=ok) as probe:
            outcome = ensure_working_model(dict(self.config), interactive=False, use_cache=True)
        self.assertEqual(probe.call_count, 1)
        self.assertFalse(outcome.cached)
        with patch.object(modelcheck, "probe_model", side_effect=AssertionError("probed")):
            outcome = ensure_working_model(dict(self.config), interactive=False, use_cache=True)
        self.assertTrue(outcome.cached)
        self.assertTrue(outcome.result.ok)

    def test_without_use_cache_nothing_is_read_or_written(self):
        ok = ProbeResult("openai", "gpt-4o", OK, "listed")
        with patch.object(modelcheck, "probe_model", return_value=ok):
            ensure_working_model(dict(self.config), interactive=False)
        self.assertFalse(verified_cache_path().exists())


class TestBackgroundStartupCheck(ModelCacheCase):
    def test_slow_probe_returns_pending_and_settles_before_the_first_message(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_probe(provider, model, config, timeout=None):
            release.wait(5)
            return ProbeResult(provider, model, OK, "listed")

        config = dict(self.config)
        with patch.object(modelcheck, "probe_model", slow_probe):
            started = time.monotonic()
            outcome = ensure_working_model(config, interactive=True, use_cache=True,
                                           background=True)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(outcome.pending)
            self.assertIsNone(outcome.result)
            check = take_route_check(config, "openai", "gpt-4o")
            self.assertIsInstance(check, RouteCheck)
            self.assertTrue(check.startup)
            release.set()
            self.assertTrue(check.wait().ok)
        # The background success is remembered for the next launch.
        self.assertIsNotNone(cached_check("openai", "gpt-4o", self.config))

    def test_instant_failure_still_gets_the_launch_recovery(self):
        bad = ProbeResult("openai", "gpt-4o", NO_KEY, "OPENAI_API_KEY is not set")
        recovered = modelcheck.ModelCheckOutcome("openai", "gpt-4o-mini", bad, switched=True)
        config = dict(self.config)
        with patch.object(modelcheck, "probe_model", return_value=bad), \
                patch.object(modelcheck, "_interactive_recovery",
                             return_value=recovered) as recovery:
            outcome = ensure_working_model(config, interactive=True, use_cache=True,
                                           background=True)
        self.assertIs(outcome, recovered)
        self.assertEqual(recovery.call_args[0][1].status, NO_KEY)
        self.assertNotIn(ROUTE_CHECK_KEY, config)

    def test_a_model_switch_during_the_startup_check_keeps_a_route_to_restore(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_probe(provider, model, config, timeout=None):
            release.wait(5)
            return ProbeResult(provider, model, OK, "listed")

        config = dict(self.config, api_key_env="OPENAI_API_KEY")
        with patch.object(modelcheck, "probe_model", slow_probe):
            ensure_working_model(config, interactive=True, use_cache=True, background=True)
            startup = config[ROUTE_CHECK_KEY]
            switch = RouteCheck.begin(config, "openai", "gpt-4.1", dict(config))
            release.set()
            switch.wait()
            startup.wait()
        self.assertEqual(switch.previous["chat_model"], "gpt-4o")

    def test_recover_startup_failure_uses_the_interactive_recovery(self):
        bad = ProbeResult("openai", "gpt-4o", TIMEOUT, "timed out after 4s")
        with patch.object(modelcheck, "_interactive_recovery",
                          side_effect=modelcheck._startup_error("none")) as recovery:
            with self.assertRaises(bootstrap.StartupError):
                modelcheck.recover_startup_failure(dict(self.config), bad, input_fn=lambda p: "q")
        self.assertFalse(recovery.call_args.kwargs["restart_hint"])


if __name__ == "__main__":
    unittest.main()
