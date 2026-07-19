"""Tests for conch_introspect: model-facing introspection of conch's own
capabilities, configuration, and source code."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conch
from conch.tooling import (
    CONCH_INTROSPECT_TOOL,
    INTROSPECT_OUTPUT_MAX,
    ConchIntrospectClient,
    LOCAL_SHELL_TOOL,
    PINNED_TOOL_NAMES,
    ToolRuntimeState,
    conch_source_root,
    set_agent_mode,
)


def _client(config=None, state=None):
    client = ConchIntrospectClient()
    if state is None:
        state = ToolRuntimeState(
            all_tools=[LOCAL_SHELL_TOOL, CONCH_INTROSPECT_TOOL],
            tool_map={},
            tools=[LOCAL_SHELL_TOOL, CONCH_INTROSPECT_TOOL],
        )
    client.bind("ollama", "qwen3.6:27b",
                config if config is not None else {"provider": "ollama"}, state)
    return client


def _call(client, args):
    return client.call_tool("conch_introspect", args)["content"][0]["text"]


class IsolatedConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        set_agent_mode(False)
        self.addCleanup(set_agent_mode, False)


class TestCapabilities(IsolatedConfigTestCase):
    def test_derived_from_live_registries(self):
        from conch.commands import SLASH_COMMANDS
        report = _call(_client(), {"action": "capabilities"})
        self.assertIn(f"Conch v{conch.__version__}", report)
        # every registered slash command name appears
        for spec, _ in SLASH_COMMANDS[:10]:
            self.assertIn(spec.split()[0], report)
        self.assertIn("local_shell", report)
        self.assertIn("## Tool profiles", report)
        self.assertIn("minimal", report)
        self.assertIn("## Providers", report)
        self.assertIn("ollama/qwen3.6:27b", report)

    def test_skills_listed_when_present(self):
        skills_dir = Path(self._tmp.name) / "conch" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "deploy-check.md").write_text(
            "---\nname: deploy-check\ndescription: Verify a deploy\n---\nsteps"
        )
        report = _call(_client(), {"action": "capabilities"})
        self.assertIn("deploy-check: Verify a deploy", report)

    def test_output_bounded(self):
        report = _call(_client(), {"action": "capabilities"})
        self.assertLessEqual(len(report), INTROSPECT_OUTPUT_MAX + 200)


class TestConfigReport(IsolatedConfigTestCase):
    def test_shows_provider_window_and_paths(self):
        config = {"provider": "ollama",
                  "ollama_base_url": "http://192.168.1.152:11434",
                  "ollama_num_ctx": "16384"}
        report = _call(_client(config=config), {"action": "config"})
        self.assertIn("ollama", report)
        self.assertIn("16,384 tokens", report)
        self.assertIn("192.168.1.152", report)
        self.assertIn("permission mode", report)
        self.assertIn("config file:", report)

    def test_secrets_hidden(self):
        config = {"provider": "ollama", "some_api_token": "sekrit-value",
                  "api_key_env": "MY_KEY_ENV"}
        report = _call(_client(config=config), {"action": "config"})
        self.assertNotIn("sekrit-value", report)
        self.assertIn("secret-like setting(s) hidden", report)
        self.assertIn("MY_KEY_ENV", report, "_env names are not secrets")


class TestSourceOverview(IsolatedConfigTestCase):
    def test_root_is_this_checkout(self):
        root = conch_source_root()
        self.assertTrue((root / "conch" / "runtime.py").is_file())
        self.assertTrue((root / ".git").exists())

    def test_overview_lists_modules_and_git_state(self):
        report = _call(_client(), {"action": "source_overview"})
        self.assertIn("git checkout", report)
        self.assertIn("branch:", report)
        self.assertIn("recent commits:", report)
        self.assertIn("conch/runtime.py", report)
        self.assertLessEqual(len(report), INTROSPECT_OUTPUT_MAX + 200)


class TestReadSource(IsolatedConfigTestCase):
    def test_reads_own_file_with_line_numbers(self):
        report = _call(_client(), {"action": "read_source", "path": "conch/memory.py"})
        self.assertIn("conch/memory.py lines 1-", report)
        self.assertIn("MemoryStore", report)
        self.assertLessEqual(len(report), INTROSPECT_OUTPUT_MAX + 200)

    def test_pagination_via_start_line(self):
        first = _call(_client(), {"action": "read_source", "path": "conch/runtime.py"})
        self.assertIn("continue with start_line=", first)
        import re
        next_line = int(re.search(r"start_line=(\d+)", first).group(1))
        second = _call(_client(), {"action": "read_source",
                                   "path": "conch/runtime.py",
                                   "start_line": next_line})
        self.assertIn(f"lines {next_line}-", second)

    def test_path_escape_rejected(self):
        for path in ("../../etc/passwd", "/etc/passwd", "conch/../../etc/hosts"):
            report = _call(_client(), {"action": "read_source", "path": path})
            self.assertIn("Error", report, path)
            self.assertNotIn("root:", report)

    def test_missing_file(self):
        report = _call(_client(), {"action": "read_source", "path": "conch/nope.py"})
        self.assertIn("no such file", report)

    def test_missing_path(self):
        report = _call(_client(), {"action": "read_source"})
        self.assertIn("Error", report)


class TestWiring(IsolatedConfigTestCase):
    def test_injected_as_builtin_and_pinned(self):
        from conch.tooling import inject_builtin_tools
        all_tools, tool_map = [], {}
        client = ConchIntrospectClient()
        inject_builtin_tools(all_tools, tool_map, {"conch_introspect": client})
        names = [t["function"]["name"] for t in all_tools]
        self.assertIn("conch_introspect", names)
        self.assertIs(tool_map["conch_introspect"], client)
        self.assertIn("conch_introspect", PINNED_TOOL_NAMES)

    def test_excluded_from_remote_sessions(self):
        from conch.remote import REMOTE_EXCLUDED_TOOLS
        self.assertIn("conch_introspect", REMOTE_EXCLUDED_TOOLS)

    def test_mentioned_in_system_prompts(self):
        from conch.prompts import get_chat_prompt
        for provider in ("ollama", "anthropic"):
            self.assertIn("conch_introspect", get_chat_prompt(provider, "m"))

    def test_completer_registry_matches_dispatcher(self):
        from conch.commands import slash_command_names
        names = slash_command_names()
        self.assertIn("/help", names)
        self.assertIn("/skill", names)
        self.assertEqual(len(names), len(set(names)), "no duplicate commands")


if __name__ == "__main__":
    unittest.main()
