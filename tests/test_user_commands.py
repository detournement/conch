"""Tests for user-defined slash commands (plan 1.7): markdown files in
~/.config/conch/commands/ become /name commands with $ARGUMENTS templates."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.commands import (
    handle_slash_command,
    load_user_commands,
    render_user_command,
)


class UserCommandsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commands_dir = Path(self._tmp.name) / "conch" / "commands"
        self.commands_dir.mkdir(parents=True)

    def _write(self, name, body):
        (self.commands_dir / f"{name}.md").write_text(body)


class TestLoadUserCommands(UserCommandsTestCase):
    def test_loads_markdown_files(self):
        self._write("review", "Review the following code:\n\n$ARGUMENTS")
        commands = load_user_commands()
        self.assertIn("review", commands)
        self.assertIn("$ARGUMENTS", commands["review"])

    def test_missing_dir_returns_empty(self):
        self.commands_dir.rmdir()
        self.assertEqual(load_user_commands(), {})

    def test_empty_and_invalid_names_skipped(self):
        self._write("empty", "   ")
        (self.commands_dir / "bad name!.md").write_text("body")
        (self.commands_dir / "notes.txt").write_text("not a command")
        commands = load_user_commands()
        self.assertEqual(commands, {})

    def test_names_lowercased(self):
        self._write("standup", "Summarize today's work")
        self.assertIn("standup", load_user_commands())


class TestRenderUserCommand(unittest.TestCase):
    def test_arguments_interpolated(self):
        self.assertEqual(
            render_user_command("Review: $ARGUMENTS", "main.py"),
            "Review: main.py",
        )

    def test_no_placeholder_appends_args(self):
        self.assertEqual(
            render_user_command("Do the thing", "with feeling"),
            "Do the thing\n\nwith feeling",
        )

    def test_no_placeholder_no_args(self):
        self.assertEqual(render_user_command("Do the thing", ""), "Do the thing")


class TestDispatch(UserCommandsTestCase):
    def _run(self, cmd):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handle_slash_command(
                cmd, {"provider": "ollama"}, "ollama", "qwen3", lambda v: None
            )
        return result

    def test_custom_command_returns_user_prompt(self):
        self._write("review", "Review the following:\n$ARGUMENTS")
        result = self._run("/review conch/app.py")
        self.assertEqual(result[0], "user_prompt")
        self.assertIn("conch/app.py", result[1])

    def test_unknown_command_still_none(self):
        self.assertIsNone(self._run("/definitely-not-a-command"))

    def test_builtin_takes_precedence(self):
        # A user file named clear.md must not shadow the builtin /clear
        self._write("clear", "custom clear prompt")
        result = self._run("/clear")
        self.assertEqual(result, "clear_conversation")


if __name__ == "__main__":
    unittest.main()
