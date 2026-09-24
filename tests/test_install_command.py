"""The /install component surface: listing, gating, and setup flows.

Components ship inside the conch-shell distribution behind config
gates; /install flips the gate, writes config, and runs the existing
daemon installers. Fleet and works arrive through the plugin component
seam; edge is foundation-wired.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod
from conch.commands import _handle_install_command, handle_slash_command


class InstallCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "config").mkdir()
        self._env = patch.dict(os.environ, {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, arg, config=None, inputs=(), getpass_values=()):
        answers = iter(inputs)
        keys = iter(getpass_values)
        out = io.StringIO()
        with patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("getpass.getpass",
                   side_effect=lambda *_: next(keys)), \
             patch("sys.stdout", out):
            _handle_install_command(arg, config if config is not None else {})
        return out.getvalue()

    def test_list_shows_all_components(self):
        out = self._run("")
        for name in ("shell", "edge", "fleet", "works"):
            self.assertIn(name, out)
        self.assertIn("disabled", out)          # edge + fleet default off
        self.assertIn("installed-unconfigured", out)  # works source module

    def test_status_reflects_enabled_gates(self):
        out = self._run("list", config={
            "edge_daemon": "true", "fleet_controller": "true",
            "capitol_base_url": "https://cap.example",
            "capitol_org": "org-1",
            "capitol_agent": "agent-1",
        })
        self.assertIn("enabled — conch-edge status", out)
        self.assertIn("enabled — /fleet status", out)
        self.assertIn("org-1", out)

    def test_unknown_component(self):
        out = self._run("warp-drive")
        self.assertIn("Unknown component", out)
        self.assertIn("/install edge", out)

    def test_install_edge_declined_writes_nothing(self):
        self._run("edge", inputs=["n"])
        self.assertFalse(Path(config_mod.get_config_path()).exists())

    def test_install_edge_enables_gate_and_runs_installer(self):
        calls = []
        with patch("conch.entrypoints.edge_main",
                   side_effect=lambda argv: calls.append(argv) or 0):
            out = self._run("edge", inputs=[""])
        self.assertEqual(calls, [["install"]])
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("edge_daemon = true", text)
        self.assertIn("Edge daemon installed", out)

    def test_install_fleet_enables_gate_and_installer(self):
        calls = []
        with patch("conch.fleet.controller.controller_install_cmd",
                   side_effect=lambda config: calls.append(True) or 0):
            out = self._run("fleet", inputs=["", ""])
        self.assertTrue(calls)
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("fleet_controller = true", text)
        self.assertIn("/fleet enroll", out)

    def test_install_works_stores_only_bearer_env_reference(self):
        out = self._run(
            "works",
            inputs=[
                "https://workflow.example",
                "https://platform.example",
                "org-1",
                "agent-1",
                "MY_CAPITOL_BEARER",
            ],
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn(
            "capitol_base_url = https://workflow.example", text,
        )
        self.assertIn(
            "capitol_platform_url = https://platform.example", text,
        )
        self.assertIn("capitol_org = org-1", text)
        self.assertIn("capitol_agent = agent-1", text)
        self.assertIn("capitol_bearer_env = MY_CAPITOL_BEARER", text)
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())
        self.assertIn("bearer bytes were not stored", out)

    def test_dispatches_from_the_slash_registry(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            result = handle_slash_command(
                "/install list", {}, "openai", "gpt-4o-mini", lambda *_: None
            )
        self.assertIsNone(result)
        self.assertIn("Conch components", out.getvalue())

    def test_registered_in_slash_commands(self):
        from conch.commands import slash_command_names

        self.assertIn("/install", slash_command_names())


if __name__ == "__main__":
    unittest.main()
