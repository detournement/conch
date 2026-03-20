"""Tests for conch.agent -- AgentSession and shared runtime."""

import unittest
from unittest.mock import patch, MagicMock

from conch.agent import _build_system_prompt, _make_builtin_clients, AgentSession


class TestBuildSystemPrompt(unittest.TestCase):
    def test_includes_date(self):
        result = _build_system_prompt("base prompt")
        self.assertIn("Current date and time:", result)

    def test_includes_location(self):
        result = _build_system_prompt("base", location="London, UK")
        self.assertIn("London, UK", result)

    def test_includes_provider_model(self):
        result = _build_system_prompt("base", provider="anthropic", model="claude-sonnet-4-6")
        self.assertIn("anthropic/claude-sonnet-4-6", result)

    def test_no_location_when_empty(self):
        result = _build_system_prompt("base", location="")
        self.assertNotIn("User location:", result)

    def test_base_prompt_preserved(self):
        result = _build_system_prompt("You are Conch.")
        self.assertTrue(result.startswith("You are Conch."))


class TestMakeBuiltinClients(unittest.TestCase):
    def test_returns_all_clients(self):
        from conch.memory import MemoryStore
        memory = MemoryStore()
        clients = _make_builtin_clients(memory)
        self.assertIn("local_shell", clients)
        self.assertIn("manage_tools", clients)
        self.assertIn("save_memory", clients)
        self.assertIn("conch_config", clients)
        self.assertIn("public_api", clients)

    def test_client_names(self):
        from conch.memory import MemoryStore
        memory = MemoryStore()
        clients = _make_builtin_clients(memory)
        self.assertEqual(clients["local_shell"].name, "local_shell")
        self.assertEqual(clients["conch_config"].name, "conch_config")
        self.assertEqual(clients["public_api"].name, "public_api")


class TestSlackImport(unittest.TestCase):
    def test_slack_module_importable(self):
        """slack.py should import without slack_bolt installed."""
        from conch import slack
        self.assertTrue(hasattr(slack, "SlackTransport"))
        self.assertTrue(hasattr(slack, "main"))


if __name__ == "__main__":
    unittest.main()
