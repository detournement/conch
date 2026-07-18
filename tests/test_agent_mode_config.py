"""Tests for the agent_mode config default — parsing, startup state, notice."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from conch.config import DEFAULT_CONFIG, get_bool, load_config
from conch.tooling import get_agent_mode, set_agent_mode


class _AgentModeStateMixin:
    """Reset global agent-mode state around each test."""

    def setUp(self):
        set_agent_mode(False)

    def tearDown(self):
        set_agent_mode(False)


class TestAgentModeConfigParsing(unittest.TestCase):
    def _load_with_config(self, contents):
        """Load config with XDG_CONFIG_HOME/HOME pointed at a temp dir."""
        with tempfile.TemporaryDirectory() as tmp:
            conch_dir = Path(tmp) / "conch"
            conch_dir.mkdir()
            if contents is not None:
                (conch_dir / "config").write_text(contents)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                with mock.patch.object(Path, "home", return_value=Path(tmp)):
                    return load_config()

    def test_default_is_off(self):
        self.assertEqual(DEFAULT_CONFIG.get("agent_mode"), "false")
        config = self._load_with_config(None)
        self.assertFalse(get_bool(config, "agent_mode"))

    def test_agent_mode_true_parsed(self):
        config = self._load_with_config("agent_mode=true\n")
        self.assertTrue(get_bool(config, "agent_mode"))

    def test_agent_mode_false_parsed(self):
        config = self._load_with_config("agent_mode=false\n")
        self.assertFalse(get_bool(config, "agent_mode"))

    def test_truthy_variants(self):
        for value in ("true", "1", "yes", "on", "TRUE", "On"):
            config = self._load_with_config(f"agent_mode={value}\n")
            self.assertTrue(get_bool(config, "agent_mode"), value)

    def test_falsy_variants(self):
        for value in ("false", "0", "no", "off", "nonsense"):
            config = self._load_with_config(f"agent_mode={value}\n")
            self.assertFalse(get_bool(config, "agent_mode"), value)

    def test_quoted_value(self):
        config = self._load_with_config('agent_mode="true"\n')
        self.assertTrue(get_bool(config, "agent_mode"))


class TestAgentModeStartup(_AgentModeStateMixin, unittest.TestCase):
    def test_config_true_enables_agent_mode(self):
        from conch.app import apply_agent_mode_from_config

        came_from_config = apply_agent_mode_from_config({"agent_mode": "true"})
        self.assertTrue(came_from_config)
        self.assertTrue(get_agent_mode())

    def test_config_false_leaves_agent_mode_off(self):
        from conch.app import apply_agent_mode_from_config

        came_from_config = apply_agent_mode_from_config({"agent_mode": "false"})
        self.assertFalse(came_from_config)
        self.assertFalse(get_agent_mode())

    def test_missing_key_leaves_agent_mode_off(self):
        from conch.app import apply_agent_mode_from_config

        came_from_config = apply_agent_mode_from_config({})
        self.assertFalse(came_from_config)
        self.assertFalse(get_agent_mode())


class TestAgentModeNotice(_AgentModeStateMixin, unittest.TestCase):
    def test_notice_text(self):
        from conch.app import AGENT_MODE_CONFIG_NOTICE

        self.assertEqual(
            AGENT_MODE_CONFIG_NOTICE,
            "agent mode is ON by default (shell commands run without confirmation) — "
            "/agent to turn it off, or remove agent_mode from ~/.config/conch/config",
        )

    def test_notice_flag_only_true_when_config_enabled(self):
        from conch.app import apply_agent_mode_from_config

        self.assertTrue(apply_agent_mode_from_config({"agent_mode": "true"}))
        # Config off → no notice, even if agent mode is already on
        # (e.g. would-be manual toggle state).
        set_agent_mode(True)
        self.assertFalse(apply_agent_mode_from_config({"agent_mode": "false"}))

    def test_manual_toggle_does_not_print_notice(self):
        from conch.app import AGENT_MODE_CONFIG_NOTICE
        from conch.commands import handle_slash_command

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = handle_slash_command(
                "/agent on", {}, "anthropic", "claude-sonnet-4-6", set_agent_mode
            )
        self.assertEqual(result, "agent_mode_changed")
        self.assertTrue(get_agent_mode())
        self.assertNotIn(AGENT_MODE_CONFIG_NOTICE, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
