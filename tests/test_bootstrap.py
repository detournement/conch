"""Bootstrap split (Swarm Phase 0): reusable startup wiring.

conch.bootstrap must be callable by headless modes: no readline, no TTY, no
prompts. chat_loop stays the interactive composition of these functions.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import bootstrap
from conch.bootstrap import (
    StartupError,
    build_agent_session,
    make_scheduled_executor,
    resolve_startup_model,
    resolve_startup_provider,
    start_remote_loop,
)
from conch.session import AgentSession
from conch.tooling import set_agent_mode


class BootstrapTestCase(unittest.TestCase):
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


class TestHeadlessImport(unittest.TestCase):
    def test_bootstrap_imports_without_readline_or_app(self):
        """A headless mode importing conch.bootstrap must not drag in the
        interactive shell or the readline module."""
        code = (
            "import sys\n"
            "import conch.bootstrap\n"
            "assert 'readline' not in sys.modules, 'readline was imported'\n"
            "assert 'conch.app' not in sys.modules, 'conch.app was imported'\n"
            "print('headless-ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(
            proc.returncode, 0,
            proc.stderr.decode("utf-8", errors="replace"),
        )
        self.assertIn(b"headless-ok", proc.stdout)


class TestResolveStartupProvider(BootstrapTestCase):
    def test_unknown_provider_raises_code_1(self):
        with self.assertRaises(StartupError) as ctx:
            resolve_startup_provider({"provider": "definitely-not-real"})
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("unknown provider", str(ctx.exception))

    def test_local_only_cloud_provider_raises_code_2(self):
        with self.assertRaises(StartupError) as ctx:
            resolve_startup_provider(
                {"provider": "openai", "local_only": "true"}
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("local_only", str(ctx.exception))

    def test_valid_provider_returns_raw_fn(self):
        provider, raw_fn = resolve_startup_provider({"provider": "openai"})
        self.assertEqual(provider, "openai")
        self.assertTrue(callable(raw_fn))

    def test_default_provider_is_openai(self):
        provider, _ = resolve_startup_provider({})
        self.assertEqual(provider, "openai")


class TestResolveStartupModel(BootstrapTestCase):
    def test_known_cloud_model_passes_without_warnings(self):
        from conch.providers import KNOWN_MODELS
        model = sorted(KNOWN_MODELS["openai"])[0]
        config = {"provider": "openai", "chat_model": model}
        resolved, warnings = resolve_startup_model(config, "openai")
        self.assertEqual(resolved, model)
        self.assertEqual(warnings, [])

    def test_unknown_cloud_model_is_replaced_with_warning(self):
        config = {"provider": "openai", "chat_model": "gpt-imaginary-99"}
        resolved, warnings = resolve_startup_model(config, "openai")
        self.assertNotEqual(resolved, "gpt-imaginary-99")
        self.assertTrue(warnings and "gpt-imaginary-99" in warnings[0])
        self.assertEqual(config["chat_model"], resolved,
                         "config must be updated in place")


class TestBuildAgentSession(BootstrapTestCase):
    def _build(self, config=None):
        with patch.object(bootstrap.mcp_mod, "create_clients", return_value={}), \
                patch.object(bootstrap.mcp_mod, "collect_tools", return_value=([], {})), \
                patch.object(bootstrap.mcp_mod, "save_tool_cache", lambda tools: None):
            return build_agent_session(
                config or {"provider": "openai", "chat_model": "gpt-4o"}
            )

    def test_returns_wired_session(self):
        session = self._build()
        self.addCleanup(session.close)
        self.assertIsInstance(session, AgentSession)
        for name in ("local_shell", "ssh_remote", "delegate_task",
                     "conch_introspect", "todo_list"):
            self.assertIn(name, session.builtin_clients)
        self.assertIsNotNone(session.chat_state)
        # Built-in tools present in the session's tool state.
        tool_names = {
            t.get("function", {}).get("name") for t in session.chat_state.tools
        }
        self.assertIn("local_shell", tool_names)

    def test_session_clients_bound_to_session_permissions(self):
        session = self._build()
        self.addCleanup(session.close)
        shell = session.builtin_clients["local_shell"]
        self.assertIs(shell.permissions(), session.permissions)
        self.assertEqual(shell._cwd, session.cwd)

    def test_delegate_task_bound_to_session(self):
        session = self._build()
        self.addCleanup(session.close)
        self.assertIs(
            session.builtin_clients["delegate_task"]._session, session
        )

    def test_two_sessions_do_not_share_state(self):
        a = self._build({"provider": "openai", "chat_model": "gpt-4o"})
        self.addCleanup(a.close)
        b = self._build({"provider": "openai", "chat_model": "gpt-4o"})
        self.addCleanup(b.close)
        self.assertIsNot(a.builtin_clients["local_shell"],
                         b.builtin_clients["local_shell"])
        self.assertIsNot(a.chat_state, b.chat_state)
        self.assertIsNot(a.permissions, b.permissions)
        a.permissions.set_agent_mode(True)
        self.assertFalse(b.permissions.get_agent_mode())


class TestScheduledExecutor(BootstrapTestCase):
    def test_executor_builds_session_runs_turn_and_closes(self):
        events = []

        class FakeSession:
            def run_turn(self, messages, max_tool_rounds=None, **kwargs):
                events.append(("run", messages[0]["content"], max_tool_rounds))
                return "reply-text", {"input_tokens": 1, "output_tokens": 2}

            def close(self):
                events.append(("close",))

        routed = []

        def fake_route(config, task, reply, usage):
            routed.append((reply, usage))

        prompts = ["prompt-v1"]
        executor = make_scheduled_executor(
            {"provider": "openai"},
            lambda: prompts[0],
            max_tool_rounds=9,
            route_output=fake_route,
        )
        with patch.object(bootstrap, "build_agent_session",
                          return_value=FakeSession()):
            reply, usage = executor("do the thing", object())
        self.assertEqual(reply, "reply-text")
        self.assertEqual(events[0], ("run", "prompt-v1", 9))
        self.assertEqual(events[-1], ("close",))
        self.assertEqual(routed, [("reply-text", usage)])

    def test_executor_closes_session_on_failure(self):
        events = []

        class FakeSession:
            def run_turn(self, messages, **kwargs):
                raise RuntimeError("backend down")

            def close(self):
                events.append("close")

        executor = make_scheduled_executor(
            {}, lambda: "sys", route_output=None
        )
        with patch.object(bootstrap, "build_agent_session",
                          return_value=FakeSession()):
            with self.assertRaises(RuntimeError):
                executor("x", object())
        self.assertEqual(events, ["close"])

    def test_system_prompt_read_per_run(self):
        seen = []

        class FakeSession:
            def run_turn(self, messages, **kwargs):
                seen.append(messages[0]["content"])
                return "", {}

            def close(self):
                pass

        prompts = ["first"]
        executor = make_scheduled_executor(
            {}, lambda: prompts[0], route_output=None
        )
        with patch.object(bootstrap, "build_agent_session",
                          return_value=FakeSession()):
            executor("a", object())
            prompts[0] = "second"
            executor("b", object())
        self.assertEqual(seen, ["first", "second"])


class TestStartRemoteLoop(BootstrapTestCase):
    def test_disabled_returns_none(self):
        loop, reason = start_remote_loop({})
        self.assertIsNone(loop)
        self.assertEqual(reason, "disabled")

    def test_enabled_without_channels_returns_reason(self):
        loop, reason = start_remote_loop({"remote_enabled": "true"})
        self.assertIsNone(loop)
        self.assertEqual(reason, "no channel configured")


if __name__ == "__main__":
    unittest.main()
