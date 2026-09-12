"""AgentSession (Swarm Phase 0): per-session policy/cwd/tool state.

The gate these tests enforce: two simultaneous sessions can never leak
permission mode, working directory, or tool-client state into each other,
and the module-level helpers keep operating on the process-default state so
the interactive CLI behaves exactly as before.
"""

import io
import tempfile
import unittest
from unittest.mock import patch

from conch.session import AgentSession, SessionBudgets
from conch.tooling import (
    LocalShellClient,
    LocalShellPolicy,
    PermissionState,
    ConchConfigClient,
    DelegateTaskClient,
    ToolRuntimeState,
    default_permissions,
    get_agent_mode,
    get_permission_mode,
    set_agent_mode,
    set_permission_mode,
)


class TestPermissionState(unittest.TestCase):
    def setUp(self):
        set_agent_mode(False)
        set_permission_mode("prompt_all")
        self.addCleanup(set_agent_mode, False)
        self.addCleanup(set_permission_mode, "prompt_all")

    def test_instances_are_independent(self):
        a = PermissionState()
        b = PermissionState()
        a.set_agent_mode(True)
        a.set_permission_mode("safe_auto")
        self.assertEqual(a.get_permission_mode(), "yolo")
        self.assertFalse(b.get_agent_mode())
        self.assertEqual(b.get_permission_mode(), "prompt_all")

    def test_module_helpers_hit_default_instance(self):
        set_agent_mode(True)
        self.assertTrue(default_permissions().get_agent_mode())
        self.assertEqual(get_permission_mode(), "yolo")
        default_permissions().set_agent_mode(False)
        self.assertFalse(get_agent_mode())

    def test_invalid_permission_mode_ignored(self):
        state = PermissionState()
        state.set_permission_mode("bogus")
        self.assertEqual(state.get_permission_mode(), "prompt_all")

    def test_session_state_never_touches_default(self):
        state = PermissionState()
        state.set_agent_mode(True)
        self.assertFalse(get_agent_mode())
        self.assertEqual(get_permission_mode(), "prompt_all")


class TestSessionPolicyIsolation(unittest.TestCase):
    """Two simultaneous sessions must not leak policy into each other."""

    def setUp(self):
        set_agent_mode(False)
        set_permission_mode("prompt_all")
        self.addCleanup(set_agent_mode, False)
        self.addCleanup(set_permission_mode, "prompt_all")

    def _shell(self, permissions):
        client = LocalShellClient(permissions=permissions)
        client.set_policy(
            LocalShellPolicy(interactive=False, allow_auto_execute=False)
        )
        return client

    def test_agent_mode_in_one_session_does_not_leak(self):
        state_a = PermissionState(agent_mode=True)
        state_b = PermissionState()
        shell_a = self._shell(state_a)
        shell_b = self._shell(state_b)
        with patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", io.StringIO()):
            result_a = shell_a.call_tool(
                "local_shell", {"command": "echo session-a-ran"}
            )
            result_b = shell_b.call_tool(
                "local_shell", {"command": "echo session-b-ran"}
            )
        self.assertIn("session-a-ran", result_a["content"][0]["text"])
        text_b = result_b["content"][0]["text"]
        self.assertNotIn("session-b-ran", text_b)
        self.assertIn("cannot prompt", text_b)
        # And the process default stayed untouched.
        self.assertFalse(get_agent_mode())

    def test_confirmation_answer_A_stays_in_session(self):
        state = PermissionState()
        client = LocalShellClient(permissions=state)
        client.set_policy(
            LocalShellPolicy(
                interactive=True,
                allow_auto_execute=False,
                input_fn=lambda prompt: "A",
            )
        )
        with patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", io.StringIO()):
            result = client.call_tool("local_shell", {"command": "echo hi"})
        self.assertIn("hi", result["content"][0]["text"])
        self.assertTrue(state.get_agent_mode())
        self.assertFalse(get_agent_mode(), "'A' must not enable global agent mode")

    def test_allow_prefixes_do_not_leak_between_sessions(self):
        shell_a = self._shell(PermissionState())
        shell_b = self._shell(PermissionState())
        shell_a.allow_prefixes(["git status"])
        self.assertTrue(shell_a._prefix_allowed("git status --short"))
        self.assertFalse(shell_b._prefix_allowed("git status --short"))

    def test_conch_config_agent_toggle_scoped_to_session(self):
        state = PermissionState()
        client = ConchConfigClient()
        client.bind("openai", "gpt-4o", {}, {})
        client.bind_permissions(state)
        client.call_tool(
            "conch_config", {"action": "set_agent_mode", "value": "on"}
        )
        self.assertTrue(state.get_agent_mode())
        self.assertFalse(get_agent_mode())


class TestSessionCwdIsolation(unittest.TestCase):
    def test_two_sessions_run_in_their_own_cwd(self):
        with tempfile.TemporaryDirectory() as dir_a, \
                tempfile.TemporaryDirectory() as dir_b:
            state = PermissionState(agent_mode=True)
            shell_a = LocalShellClient(permissions=state)
            shell_b = LocalShellClient(permissions=state)
            shell_a.set_policy(LocalShellPolicy(interactive=False))
            shell_b.set_policy(LocalShellPolicy(interactive=False))
            shell_a.set_cwd(dir_a)
            shell_b.set_cwd(dir_b)
            with patch("sys.stderr", io.StringIO()), \
                    patch("sys.stdout", io.StringIO()):
                out_a = shell_a.call_tool("local_shell", {"command": "pwd"})
                out_b = shell_b.call_tool("local_shell", {"command": "pwd"})
            text_a = out_a["content"][0]["text"]
            text_b = out_b["content"][0]["text"]
            # Resolve symlinks (macOS /var -> /private/var) via realpath.
            import os
            self.assertIn(os.path.basename(dir_a), text_a)
            self.assertIn(os.path.basename(dir_b), text_b)
            self.assertNotEqual(text_a.strip(), text_b.strip())

    def test_unset_cwd_preserves_process_cwd_behavior(self):
        import os
        state = PermissionState(agent_mode=True)
        shell = LocalShellClient(permissions=state)
        shell.set_policy(LocalShellPolicy(interactive=False))
        with patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", io.StringIO()):
            out = shell.call_tool("local_shell", {"command": "pwd"})
        self.assertIn(
            os.path.basename(os.getcwd()), out["content"][0]["text"]
        )


class TestAgentSessionObject(unittest.TestCase):
    def setUp(self):
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)

    def test_defaults(self):
        session = AgentSession({"provider": "openai", "model": "gpt-4o"})
        self.assertEqual(session.provider, "openai")
        self.assertEqual(session.model, "gpt-4o")
        self.assertIsInstance(session.permissions, PermissionState)
        self.assertIsNot(session.permissions, default_permissions())
        self.assertEqual(session.budgets.max_tool_rounds, 25)
        self.assertIsNone(session.budgets.turn_token_budget)

    def test_two_sessions_own_separate_state(self):
        config_a = {"provider": "openai", "chat_model": "gpt-4o"}
        config_b = {"provider": "anthropic", "chat_model": "claude"}
        a = AgentSession(config_a)
        b = AgentSession(config_b)
        a.permissions.set_agent_mode(True)
        a.budgets.max_tool_rounds = 3
        config_a["chat_model"] = "gpt-4o-mini"
        self.assertFalse(b.permissions.get_agent_mode())
        self.assertEqual(b.budgets.max_tool_rounds, 25)
        self.assertEqual(b.model, "claude")
        self.assertEqual(a.model, "gpt-4o-mini")

    def test_attach_clients_binds_session_state(self):
        session = AgentSession(
            {"provider": "openai"}, cwd="/tmp",
            permissions=PermissionState(agent_mode=True),
        )
        shell = LocalShellClient()
        session.attach_clients({"local_shell": shell})
        self.assertIs(shell.permissions(), session.permissions)
        self.assertEqual(shell._cwd, "/tmp")

    def test_attach_clients_bind_false_keeps_parent_bindings(self):
        parent_state = PermissionState()
        shell = LocalShellClient(permissions=parent_state)
        child = AgentSession(
            {"provider": "openai"},
            permissions=PermissionState(agent_mode=True),
        )
        child.attach_clients({"local_shell": shell}, bind=False)
        self.assertIs(shell.permissions(), parent_state)

    def test_run_turn_wraps_chat_turn(self):
        session = AgentSession(
            {"provider": "openai", "chat_model": "gpt-4o"},
            budgets=SessionBudgets(max_tool_rounds=7),
        )
        state = ToolRuntimeState(all_tools=[], tool_map={"x": object()}, tools=[])
        session.attach_clients({}, chat_state=state)
        seen = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, max_tool_rounds=25,
                           chat_state=None, on_token=None, input_fn=None):
            seen.update(
                provider=provider, tools=tools, tool_map=tool_map,
                max_tool_rounds=max_tool_rounds, chat_state=chat_state,
                config=config,
            )
            return "ok", {"input_tokens": 1, "output_tokens": 1, "model": "m"}

        with patch("conch.runtime.chat_turn", fake_chat_turn):
            reply, usage = session.run_turn(
                [{"role": "user", "content": "hi"}]
            )
        self.assertEqual(reply, "ok")
        self.assertEqual(seen["provider"], "openai")
        self.assertEqual(seen["max_tool_rounds"], 7)
        self.assertIs(seen["chat_state"], state)
        self.assertIs(seen["tool_map"], state.tool_map)

    def test_run_turn_token_budget_override_copies_config(self):
        config = {"provider": "openai", "turn_token_budget": 100}
        session = AgentSession(
            config, budgets=SessionBudgets(turn_token_budget=5)
        )
        seen = {}

        def fake_chat_turn(cfg, *args, **kwargs):
            seen["config"] = cfg
            return "", {}

        with patch("conch.runtime.chat_turn", fake_chat_turn):
            session.run_turn([])
        self.assertEqual(seen["config"]["turn_token_budget"], 5)
        self.assertEqual(config["turn_token_budget"], 100,
                         "session budget override must not mutate the config")

    def test_run_turn_unknown_provider_raises(self):
        session = AgentSession({"provider": "not-a-provider"})
        with self.assertRaises(RuntimeError):
            session.run_turn([])

    def test_close_is_idempotent_and_closes_ssh(self):
        closed = []

        class _FakeSSH:
            def close(self):
                closed.append(True)

        session = AgentSession({"provider": "openai"})
        session.attach_clients({"ssh_remote": _FakeSSH()}, bind=False)
        session.close()
        session.close()
        self.assertEqual(closed, [True])


class TestDelegateChildSession(unittest.TestCase):
    """delegate_task builds child sessions sharing the parent's authority."""

    def setUp(self):
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)

    def test_child_session_shares_parent_permissions(self):
        parent = AgentSession(
            {"provider": "openai", "chat_model": "gpt-4o"},
            permissions=PermissionState(),
        )
        parent.attach_clients(
            {"local_shell": LocalShellClient()},
            chat_state=ToolRuntimeState(all_tools=[], tool_map={}, tools=[]),
        )
        client = DelegateTaskClient()
        client.bind_session(parent)
        captured = {}

        def fake_chat_turn(config, provider, raw_fn, messages, tools,
                           tool_map, builtin_clients, max_tool_rounds=25,
                           chat_state=None, on_token=None, input_fn=None):
            shell = builtin_clients.get("local_shell")
            captured["permissions"] = shell.permissions() if shell else None
            captured["rounds"] = max_tool_rounds
            return "did it", {"input_tokens": 1, "output_tokens": 1}

        with patch("conch.runtime.chat_turn", fake_chat_turn), \
                patch("sys.stderr", io.StringIO()), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = client.call_tool("delegate_task", {"task": "do a thing"})
        self.assertIn("did it", result["content"][0]["text"])
        self.assertIs(captured["permissions"], parent.permissions)

    def test_legacy_bind_still_works(self):
        state = ToolRuntimeState(all_tools=[], tool_map={}, tools=[])
        client = DelegateTaskClient()
        client.bind({"provider": "openai"}, state, {})

        def fake_chat_turn(*args, **kwargs):
            return "legacy ok", {"input_tokens": 0, "output_tokens": 0}

        with patch("conch.runtime.chat_turn", fake_chat_turn), \
                patch("sys.stderr", io.StringIO()), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = client.call_tool("delegate_task", {"task": "t"})
        self.assertIn("legacy ok", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
