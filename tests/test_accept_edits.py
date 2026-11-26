"""accept_edits permission mode: file edits inside the repository apply
without asking (diff still shown); everything else keeps asking."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import tooling
from tests.test_terminal_ui_rendering import _DirectUI
from conch.config import PROJECT_RC_ALLOWED_KEYS
from conch.filetools import FileToolsClient, accept_edits_block_reason, edit_root
from conch.settings_registry import BY_KEY
from conch.tooling import (
    PERMISSION_MODES,
    LocalShellClient,
    LocalShellPolicy,
    PermissionState,
)


class _Case(unittest.TestCase):
    def setUp(self):
        top = tempfile.TemporaryDirectory()
        self.addCleanup(top.cleanup)
        self.top = Path(os.path.realpath(top.name))
        self.repo = self.top / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("value = 1\n")
        self.outside = self.top / "elsewhere"
        self.outside.mkdir()
        (self.outside / "other.txt").write_text("outside\n")
        self.prompts = []
        self.answer = "n"

    def ask(self, prompt):
        if "? [" in prompt:  # the approval question, not the reason follow-up
            self.prompts.append(prompt)
        return self.answer

    def files(self, mode="accept_edits", interactive=True, cwd=None):
        client = FileToolsClient(permissions=PermissionState(permission_mode=mode),
                                 config={})
        client.set_policy(LocalShellPolicy(interactive=interactive, input_fn=self.ask))
        client.set_cwd(str(cwd or self.repo / "src"))
        return client

    def call(self, client, name, **arguments):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = client.call_tool(name, arguments)
        self.printed = out.getvalue()
        return result["content"][0]["text"]


class AcceptEditsFileTests(_Case):
    def test_write_and_edit_inside_the_repo_apply_without_asking(self):
        client = self.files()
        text = self.call(client, "write_file", path="new.py", content="x = 2\n")
        self.assertIn("Created", text)
        self.assertIn("+x = 2", self.printed)       # the diff is still shown
        self.assertIn("accept edits", self.printed)
        self.call(client, "read_file", path="app.py")
        text = self.call(client, "edit_file", path="app.py",
                         old_string="value = 1", new_string="value = 3")
        self.assertIn("Edited", text)
        self.assertEqual((self.repo / "src" / "app.py").read_text(), "value = 3\n")
        # A path elsewhere in the repo (above the cwd) is still inside.
        self.call(client, "write_file", path="../README.md", content="hi\n")
        self.assertTrue((self.repo / "README.md").exists())
        self.assertEqual(self.prompts, [])

    def test_read_before_edit_guard_stays_in_force(self):
        client = self.files()
        text = self.call(client, "edit_file", path="app.py",
                         old_string="value = 1", new_string="value = 9")
        self.assertIn("has not been read", text)
        self.assertEqual((self.repo / "src" / "app.py").read_text(), "value = 1\n")
        self.assertEqual(self.prompts, [])

    def test_outside_the_repo_asks(self):
        client = self.files()
        self.call(client, "write_file", path=str(self.outside / "new.txt"), content="x\n")
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse((self.outside / "new.txt").exists())
        self.assertIn("outside the repository", self.printed)

    def test_guarded_paths_inside_the_repo_ask(self):
        for rel in (".env", ".env.local", ".conchrc", ".git/config", ".git/hooks/pre-commit",
                    "deploy/id_rsa", "certs/server.pem"):
            with self.subTest(path=rel):
                self.prompts.clear()
                client = self.files(cwd=self.repo)
                self.call(client, "write_file", path=rel, content="x\n")
                self.assertEqual(len(self.prompts), 1, rel)
                self.assertFalse((self.repo / rel).exists())

    def test_symlink_escaping_the_repo_counts_as_outside(self):
        (self.repo / "linkdir").symlink_to(self.outside, target_is_directory=True)
        (self.repo / "linkfile.txt").symlink_to(self.outside / "other.txt")
        client = self.files(cwd=self.repo)
        self.call(client, "write_file", path="linkdir/planted.txt", content="x\n")
        self.assertFalse((self.outside / "planted.txt").exists())
        self.call(client, "read_file", path="linkfile.txt")
        self.call(client, "edit_file", path="linkfile.txt",
                  old_string="outside", new_string="changed")
        self.assertEqual((self.outside / "other.txt").read_text(), "outside\n")
        self.assertEqual(len(self.prompts), 2)

    def test_symlink_inside_the_repo_is_inside(self):
        (self.repo / "alias").symlink_to(self.repo / "src", target_is_directory=True)
        client = self.files(cwd=self.repo)
        self.call(client, "write_file", path="alias/made.py", content="y = 1\n")
        self.assertTrue((self.repo / "src" / "made.py").exists())
        self.assertEqual(self.prompts, [])

    def test_without_a_repo_the_working_directory_is_the_root(self):
        plain = self.top / "plain"
        (plain / "sub").mkdir(parents=True)
        client = self.files(cwd=plain)
        self.call(client, "write_file", path="sub/a.txt", content="a\n")
        self.assertTrue((plain / "sub" / "a.txt").exists())
        self.call(client, "write_file", path="../sibling.txt", content="b\n")
        self.assertFalse((self.top / "sibling.txt").exists())
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(edit_root(str(plain / "sub")), str(plain / "sub"))
        self.assertEqual(edit_root(str(self.repo / "src")), str(self.repo))

    def test_session_that_cannot_ask_refuses_outside(self):
        client = self.files(interactive=False)
        inside = self.call(client, "write_file", path="ok.py", content="z\n")
        outside = self.call(client, "write_file", path=str(self.outside / "n.txt"),
                            content="z\n")
        self.assertIn("Created", inside)
        self.assertIn("Refused", outside)
        self.assertFalse((self.outside / "n.txt").exists())

    def test_other_modes_are_unchanged(self):
        for mode in ("prompt_all", "safe_auto"):
            with self.subTest(mode=mode):
                self.prompts.clear()
                client = self.files(mode=mode)
                self.call(client, "write_file", path="m.py", content="m\n")
                self.assertEqual(len(self.prompts), 1)
                self.assertFalse((self.repo / "src" / "m.py").exists())

    def test_block_reason_helper(self):
        cwd = str(self.repo / "src")
        self.assertIsNone(accept_edits_block_reason(str(self.repo / "src" / "x.py"), cwd))
        self.assertIn("outside", accept_edits_block_reason(str(self.outside / "x"), cwd))
        self.assertIn(".git", accept_edits_block_reason(str(self.repo / ".git" / "HEAD"), cwd))


class AcceptEditsShellTests(_Case):
    def shell(self):
        client = LocalShellClient(permissions=PermissionState(permission_mode="accept_edits"))
        client.set_policy(LocalShellPolicy(interactive=True, input_fn=self.ask))
        client.set_cwd(str(self.repo))
        return client

    def test_mutating_commands_still_ask(self):
        client = self.shell()
        self.call(client, "local_shell", command="touch made-by-shell")
        self.assertEqual(len(self.prompts), 1)
        self.assertFalse((self.repo / "made-by-shell").exists())

    def test_read_only_commands_run_like_safe_auto(self):
        client = self.shell()
        text = self.call(client, "local_shell", command="ls src")
        self.assertIn("app.py", text)
        self.assertEqual(self.prompts, [])

    def test_web_fetch_auto_approves_like_safe_auto(self):
        from conch.webfetch import WebFetchClient

        client = WebFetchClient(permissions=PermissionState(permission_mode="accept_edits"),
                                config={})
        client.set_policy(LocalShellPolicy(interactive=True, input_fn=self.ask))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(client._approve("https://example.com/", "example.com"))
        self.assertEqual(self.prompts, [])


class ModeSelectionTests(unittest.TestCase):
    def setUp(self):
        default = tooling.default_permissions()
        saved = (default.get_agent_mode(), default._permission_mode)

        def restore():
            default.set_agent_mode(saved[0])
            default._permission_mode = saved[1]

        self.addCleanup(restore)

    def test_mode_is_known_and_normalized(self):
        self.assertIn("accept_edits", PERMISSION_MODES)
        state = PermissionState(permission_mode="Accept-Edits")
        self.assertEqual(state.get_permission_mode(), "accept_edits")
        state.set_agent_mode(True)
        self.assertEqual(state.get_permission_mode(), "yolo")

    def test_config_setting_offers_it_and_project_rc_cannot_set_it(self):
        self.assertIn("accept_edits", BY_KEY["permission_mode"].choices)
        self.assertNotIn("permission_mode", PROJECT_RC_ALLOWED_KEYS)

    def test_config_default_applies_at_startup(self):
        from conch.bootstrap import apply_agent_mode_from_config

        apply_agent_mode_from_config({"permission_mode": "accept_edits"})
        self.assertEqual(tooling.get_permission_mode(), "accept_edits")

    def test_permissions_slash_command_sets_and_shows_the_mode(self):
        from conch.commands import BUILTIN_SLASH_COMMANDS, handle_slash_command

        self.assertIn("/permissions", BUILTIN_SLASH_COMMANDS)
        tooling.set_agent_mode(True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handle_slash_command(
                "/permissions accept-edits", {"provider": "ollama"}, "ollama", "m",
                tooling.set_agent_mode)
        self.assertEqual(result, "agent_mode_changed")
        self.assertFalse(tooling.get_agent_mode())
        self.assertEqual(tooling.get_permission_mode(), "accept_edits")
        self.assertIn("accept_edits", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            bad = handle_slash_command("/permissions sideways", {"provider": "ollama"},
                                       "ollama", "m", tooling.set_agent_mode)
        self.assertIsNone(bad)
        self.assertEqual(tooling.get_permission_mode(), "accept_edits")

    def test_fan_out_workers_inherit_accept_edits(self):
        from conch import fanout

        captured = []
        real = tooling.PermissionState

        def record(**kwargs):
            state = real(**kwargs)
            captured.append(state.get_permission_mode())
            return state

        with patch("conch.tooling.PermissionState", side_effect=record), \
                patch("conch.bootstrap.make_builtin_clients",
                      side_effect=RuntimeError("stop here")):
            result = fanout.run_envelope({"permission_mode": "accept_edits",
                                          "config": {}, "task": "t"})
        self.assertFalse(result["ok"])
        self.assertEqual(captured[0], "accept_edits")


class StatusBarTests(_DirectUI):
    async def test_status_bar_names_the_mode(self):
        self.ui.set_context(permission="accept_edits")
        self.assertIn("accept edits", self.render())


if __name__ == "__main__":
    unittest.main()
