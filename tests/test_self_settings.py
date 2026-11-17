"""conch changing its own settings when asked (conch_config list_settings /
set_setting / reset_setting) and looking up how its features work
(conch_introspect docs / settings)."""

import io
import os
import tempfile
import unittest
from unittest import mock

from conch.tooling import (
    ConchConfigClient,
    ConchIntrospectClient,
    LocalShellPolicy,
    PermissionState,
)


def _text(result):
    return result["content"][0]["text"]


class SelfSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = {"provider": "anthropic", "chat_model": "claude-sonnet-5"}
        self.state = PermissionState()
        self.answers = []
        self.client = ConchConfigClient()
        self.client.bind("anthropic", "claude-sonnet-5", {}, self.config)
        self.client.bind_permissions(self.state)

    def answer(self, reply):
        def ask(prompt):
            self.answers.append(prompt)
            return reply
        self.client.set_policy(LocalShellPolicy(interactive=True, input_fn=ask))

    def call(self, **arguments):
        with mock.patch("sys.stdout", io.StringIO()):
            return _text(self.client.call_tool("conch_config", arguments))

    def saved(self):
        from conch.config import load_config

        return load_config()

    def test_plain_setting_saves_and_applies_without_asking(self):
        self.answer("n")
        out = self.call(action="set_setting", key="notify_after_seconds", value="off")
        self.assertIn("notify_after_seconds = 0", out)
        self.assertEqual(self.answers, [])
        self.assertEqual(self.config["notify_after_seconds"], "0")
        self.assertEqual(self.saved()["notify_after_seconds"], "0")

    def test_invalid_value_changes_nothing(self):
        out = self.call(action="set_setting", key="fanout_concurrency", value="99")
        self.assertIn("Not changed", out)
        self.assertNotIn("fanout_concurrency", self.config)

    def test_widening_setting_needs_the_users_yes(self):
        self.answer("n")
        out = self.call(action="set_setting", key="permission_mode", value="yolo")
        self.assertIn("declined", out)
        self.assertEqual(self.state.get_permission_mode(), "prompt_all")
        self.answer("y")
        self.call(action="set_setting", key="permission_mode", value="yolo")
        self.assertEqual(self.state.get_permission_mode(), "yolo")
        self.assertEqual(self.saved()["permission_mode"], "yolo")
        self.call(action="reset_setting", key="permission_mode")
        self.assertEqual(self.state.get_permission_mode(), "prompt_all")

    def test_nobody_to_ask_means_no(self):
        self.client.set_policy(LocalShellPolicy(interactive=False))
        out = self.call(action="set_setting", key="web_fetch_allow_domains", value="example.com")
        self.assertIn("Refused", out)
        self.assertNotIn("web_fetch_allow_domains", self.config)
        out = self.call(action="set_agent_mode", value="on")
        self.assertIn("Refused", out)
        self.assertFalse(self.state.get_agent_mode())
        self.assertIn("OFF", self.call(action="set_agent_mode", value="off"))

    def test_side_task_model_writes_the_weak_model_pair(self):
        self.call(action="set_setting", key="side_task_model", value="openai/gpt-6-luna")
        saved = self.saved()
        self.assertEqual((saved["weak_provider"], saved["weak_model"]), ("openai", "gpt-6-luna"))
        out = self.call(action="set_setting", key="side_task_model", value="openai/no-such")
        self.assertIn("Not changed", out)


class IntrospectDocsTests(unittest.TestCase):
    def setUp(self):
        self.client = ConchIntrospectClient()
        self.client.bind("anthropic", "claude-sonnet-5", {}, None)

    def call(self, **arguments):
        return _text(self.client.call_tool("conch_introspect", arguments))

    def test_docs_find_the_section_that_explains_a_feature(self):
        out = self.call(action="docs", query="model picker")
        self.assertTrue(out.startswith("## "))
        self.assertIn("Model picker", out.splitlines()[0])

    def test_settings_list_what_conch_config_can_change(self):
        out = self.call(action="settings")
        from conch.settings_registry import SETTINGS

        for setting in SETTINGS:
            self.assertIn(setting.key, out)


if __name__ == "__main__":
    unittest.main()
